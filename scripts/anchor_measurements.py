import logging
import os
import sys
from concurrent.futures import Future, as_completed
from datetime import datetime, timezone
from itertools import batched
from typing import Generator

import pandas as pd
from requests import Response
from requests.adapters import HTTPAdapter
from requests_futures.sessions import FuturesSession
from urllib3.util import Retry

API_BASE = 'https://atlas.ripe.net/api/v2'
# Minimum number of results required for a measurement to be counted as successful.
# There are probes that claim to always reach the target, which is why the number should
# be greater than 1.
SUCCESS_THRESHOLD = 3


class AnchorChecker():
    def __init__(self) -> None:
        self.stats_file = 'stats/anchor-measurements.csv'
        self.date = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        # Anchor ID (as used in the /anchors endpoint) to probe ID (/probes endpoint)
        self.anchor_id_to_prb_id = dict()
        # Reverse of above.
        self.prb_id_to_anchor_id = dict()
        # Anchor ID to connectivity status text
        self.anchor_id_to_status = dict()
        # All measurement IDs of the anchor measurements (as used in the /measurements
        # endpoint, not /anchor-measurements)
        self.all_msm_ids = list()
        # Measurement ID to targeted anchor (as probe ID)
        self.msm_id_to_prb_id = dict()
        # If there is no anchor ID to probe ID mapping for an anchor, map the
        # measurement ID to the anchor ID instead
        self.msm_id_to_unknown_anchor_id = dict()
        # Measurement ID to is_mesh boolean from measurement metadata
        self.msm_id_is_mesh = dict()
        # Measurement IDs of ongoing measurements to their metadata. These are the
        # measurements for which we fetch results.
        # They are further subdivided by the next three members.
        self.ongoing_msm_id_to_metadata = dict()
        # The next three map measurement ID to anchor ID (although the anchor ID is not
        # used at the moment)
        # Ongoing measurements to anchors, which have no entry in the /anchors endpoint
        self.ongoing_measurements_to_unknown_anchors = dict()
        # Ongoing measurements to anchors that are not in the "Connected" state
        self.ongoing_measurements_to_nonconnected_anchors = dict()
        # Ongoing measurements to connected anchors
        self.ongoing_measurements_to_connected_anchors = dict()
        # Measurement ID to bool indicated if measurement succeeded (i.e., >=
        # SUCCESS_THRESHOLD results) or not
        self.measurement_succeeded = dict()
        # The next four members are lists of measurement IDs that require investigation.
        # Measurements fail even though the target anchor is connected
        self.failing_measurements_to_connected_anchors = list()
        # Measurements succeed even though the target anchor is disconnected
        self.succeeding_measurements_to_nonconnected_anchors = list()
        # Measurements fail and the target anchor is disconnected. Expected behavior,
        # but measurements should be stopped
        self.failing_measurements_to_nonconnected_anchors = list()
        # Measurements to anchors without /anchors endpoint entry
        self.measurements_to_unknown_anchors = list()
        # Keep track of disconnected anchors (using probe ID) and since when they are disconnected
        self.disconnected_anchors = pd.DataFrame()

        # Fetch results in parallel. GitHub workers have 4 cores.
        self.session = FuturesSession(max_workers=4)
        retries = Retry(
            total=3,
            backoff_factor=0.1,
            status_forcelist=[502, 503, 504],
        )
        self.session.mount('https://', HTTPAdapter(max_retries=retries))

    @staticmethod
    def handle_future(f: Future) -> list:
        """Try to retrieve the result of the future, check the HTTP response and decode
        JSON."""
        try:
            r: Response = f.result()
        except Exception as e:
            logging.error(f'Future failed: {e}')
            return list()
        try:
            r.raise_for_status()
            r_json = r.json()
        except Exception as e:
            logging.error(f'Request to {r.url} failed: {e}')
            return list()
        return r_json

    def fetch_url(self, url: str, params: dict = dict()):
        """Fetch single URL and return decoded JSON result."""
        logging.debug(f'Fetching {url}')
        future = self.session.get(url, params=params)
        return self.handle_future(future)

    def fetch_url_with_params_in_parallel(self, url: str, params_list: list) -> Generator[list]:
        """Fetch a single URL with multiple parameter sets in parallel.

        The base URL is the same for all requests. Generate decoded JSON results.
        Results can be empty in case of errors.

        Args:
            url (str): Base URL
            params_list (list): List of parameter dictionaries

        Yields:
            Generator[list]: Decoded JSON results
        """
        queries = list()
        for params in params_list:
            queries.append(self.session.get(url, params=params))
        for future in as_completed(queries):
            yield self.handle_future(future)

    def fetch_paginated_url(self, url: str) -> list:
        """Fetch a URL and follow the pagination until the end.

        Return concatenated results.
        """
        res = list()
        page = self.fetch_url(url, {'format': 'json', 'page_size': 500})
        if not page:
            return list()
        res.extend(page['results'])
        while page['next']:
            page = self.fetch_url(page['next'])
            if not page:
                return list()
            res.extend(page['results'])
        return res

    def fetch_api_endpoint_by_ids(self, endpoint: str, ids: list) -> list:
        """Fetch API endpoint with ID lists in parallel.

        Each item in "ids" should be a list of IDs, which will be used for the id__in
        parameter.

        Args:
            endpoint (str): API endpoint
            ids (list): List of ID lists

        Returns:
            list: Decoded JSON results
        """
        res = list()
        # Request URL gets too long if we include more ids, so no reason to use
        # pagination.
        params_list = list()
        for id_batch in batched(ids, 500):
            params_list.append({'format': 'json',
                                'page_size': 500,
                                'id__in': ','.join(map(str, id_batch))})
        for page in self.fetch_url_with_params_in_parallel(os.path.join(API_BASE, endpoint), params_list):
            if not page:
                continue
            res.extend(page['results'])
        return res

    def fetch_latest_measurement_results(self, msm_id: int) -> list:
        """Fetch the /latest endpoint for the specified measurement ID."""
        page = self.fetch_url(os.path.join(API_BASE, f'/measurements/{msm_id}/latest'),
                              {'format': 'json'},
                              )
        return page

    def process_anchor_metadata(self):
        """Fetch metadata from /anchors endpoint and create anchor/probe ID mappings."""
        anchor_metadata = self.fetch_paginated_url(os.path.join(API_BASE, 'anchors'))
        self.anchor_id_to_prb_id = {e['id']: e['probe'] for e in anchor_metadata}
        self.prb_id_to_anchor_id = {e['probe']: e['id'] for e in anchor_metadata}

    def process_anchor_measurements(self):
        """Fetch /anchor-measurements endpoint, extract measurement IDs and the anchor
        they target."""
        anchor_measurements = self.fetch_paginated_url(os.path.join(API_BASE, 'anchor-measurements'))
        for entry in anchor_measurements:
            anchor_id = int(entry['target'].split('anchors')[1].split('/')[1])
            msm_id = int(entry['measurement'].split('measurements')[1].split('/')[1])
            self.all_msm_ids.append(msm_id)
            self.msm_id_is_mesh[msm_id] = entry['is_mesh']
            if anchor_id not in self.anchor_id_to_prb_id:
                self.msm_id_to_unknown_anchor_id[msm_id] = anchor_id
                continue
            self.msm_id_to_prb_id[msm_id] = self.anchor_id_to_prb_id[anchor_id]

    def process_anchor_probe_metadata(self):
        """Fetch metadata from the /probes endpoint for all anchors and check their
        connectivity status."""
        anchor_probe_metadata = self.fetch_api_endpoint_by_ids('probes', list(self.prb_id_to_anchor_id))
        self.anchor_id_to_status = {self.prb_id_to_anchor_id[e['id']]: e['status']['name']
                                    for e in anchor_probe_metadata}
        disconnected_anchors_list = [(e['id'], e['status']['since'])
                                     for e in anchor_probe_metadata
                                     if e['status']['name'] == 'Disconnected']
        self.disconnected_anchors = pd.DataFrame(disconnected_anchors_list, columns=['prb_id', 'since'])
        # Sort by timestamp so the longest disconnected anchors come first.
        self.disconnected_anchors.sort_values('since', inplace=True)

    def process_measurement_metadata(self):
        """Fetch metadata for relevant measurements, check that they are ongoing (for
        sanity) and categorize based on target anchor connectivity."""
        # Fetch metadata for relevant measurements and check that they are ongoing for
        # sanity.
        msm_metadata = self.fetch_api_endpoint_by_ids('measurements', self.all_msm_ids)
        for msm in msm_metadata:
            msm_id = msm['id']
            msm_status = msm['status']['name']
            if msm_id in self.msm_id_to_unknown_anchor_id:
                if msm_status == 'Ongoing':
                    self.ongoing_msm_id_to_metadata[msm_id] = msm
                    self.ongoing_measurements_to_unknown_anchors[msm_id] = self.msm_id_to_unknown_anchor_id[msm_id]
                continue
            anchor_id = self.prb_id_to_anchor_id[self.msm_id_to_prb_id[msm_id]]
            if msm_status != 'Ongoing':
                if msm_status != 'Stopped':
                    # Unexpected
                    logging.info(f'Anchor measurement {msm_id} has status: {msm_status}')
                continue
            self.ongoing_msm_id_to_metadata[msm_id] = msm
            if self.anchor_id_to_status[anchor_id] != 'Connected':
                self.ongoing_measurements_to_nonconnected_anchors[msm_id] = anchor_id
                continue
            self.ongoing_measurements_to_connected_anchors[msm_id] = anchor_id

    def process_measurement_results(self):
        """Fetch latest measurement results and check if measurements succeeded."""
        queries = list()
        for msm_id in list(self.ongoing_msm_id_to_metadata):
            future = self.session.get(os.path.join(API_BASE, f'/measurements/{msm_id}/latest'),
                                      params={'format': 'json'})
            future.msm_id = msm_id
            queries.append(future)
        for future in as_completed(queries):
            res = self.handle_future(future)
            if not res:
                continue
            msm_id = future.msm_id
            metadata = self.ongoing_msm_id_to_metadata[msm_id]
            self.measurement_succeeded[msm_id] = self.eval_msm(metadata['type'], res)

    def process_measurement_succeeded(self):
        """Confirm that number of measurement results is above threshold, calculate some
        stats and categorize measurement accordingly."""
        for msm_id, (good, succeed_count, fail_count) in self.measurement_succeeded.items():
            metadata = self.ongoing_msm_id_to_metadata[msm_id]
            prb_id = self.msm_id_to_prb_id.get(msm_id, 0)
            succeed_ratio = 0
            total_results = succeed_count + fail_count
            if total_results:
                succeed_ratio = succeed_count / total_results
            line = [
                msm_id,
                prb_id,
                self.msm_id_is_mesh[msm_id],
                metadata['type'],
                metadata['af'],
                succeed_count,
                fail_count,
                succeed_ratio
            ]
            if not good and msm_id in self.ongoing_measurements_to_connected_anchors:
                self.failing_measurements_to_connected_anchors.append(line)
            elif msm_id in self.ongoing_measurements_to_nonconnected_anchors:
                if good:
                    self.succeeding_measurements_to_nonconnected_anchors.append(line)
                else:
                    self.failing_measurements_to_nonconnected_anchors.append(line)
            elif msm_id in self.ongoing_measurements_to_unknown_anchors:
                self.measurements_to_unknown_anchors.append(line)

    def write_data_files(self):
        """Write detailed data files containing individual measurement IDs."""
        out_path = 'data/anchor-measurements'
        out_file_template = self.date.strftime('%Y%m%d') + '.{name}.csv'
        # Helper loop to write files to their correct location.
        for name, data in [
            (
                'failing-measurements-to-connected-anchors',
                self.make_df(self.failing_measurements_to_connected_anchors)
            ),
            (
                'succeeding-measurements-to-nonconnected-anchors',
                self.make_df(self.succeeding_measurements_to_nonconnected_anchors)
            ),
            (
                'failing-measurements-to-nonconnected-anchors',
                self.make_df(self.failing_measurements_to_nonconnected_anchors)
            ),
            (
                'measurements-to-unknown-anchors',
                self.make_df(self.measurements_to_unknown_anchors)
            ),
            (
                'disconnected-anchors',
                self.disconnected_anchors
            )
        ]:
            tmp_path = os.path.join(out_path, name)
            os.makedirs(tmp_path, exist_ok=True)
            out_file = os.path.join(tmp_path, out_file_template.format(name=name.replace('-', '_')))
            logging.info(out_file)
            data.to_csv(out_file, index=False)

    def write_stats_file(self):
        """Write to aggregated stat file which contains one line per run to give an
        overview."""
        stats = {
            'date': self.date,
            'disconnected_anchors': len(self.disconnected_anchors),
            'failing_measurements_to_connected_anchors': len(self.failing_measurements_to_connected_anchors),
            'succeeding_measurements_to_nonconnected_anchors': len(self.succeeding_measurements_to_nonconnected_anchors),
            'failing_measurements_to_nonconnected_anchors': len(self.failing_measurements_to_nonconnected_anchors),
            'measurements_to_unknown_anchors': len(self.measurements_to_unknown_anchors)
        }
        if not os.path.exists(self.stats_file):
            stats_df = pd.DataFrame(stats, index=[0])
        else:
            stats_df = pd.read_csv(self.stats_file)
            stats_df.loc[-1] = stats
        stats_df.to_csv(self.stats_file, index=False)

    @staticmethod
    def make_df(data: list) -> pd.DataFrame:
        """Helper function to create DataFrames and sort by specific fields."""
        df = pd.DataFrame(data,
                          columns=['msm_id',
                                   'target_anchor_prb_id',
                                   'is_mesh',
                                   'msm_type',
                                   'af',
                                   'succeed_count',
                                   'fail_count',
                                   'succeed_ratio'])
        df.sort_values(['target_anchor_prb_id', 'msm_type', 'af', 'is_mesh'], inplace=True)
        return df

    @staticmethod
    def eval_traceroute(results: list):
        """Evaluate traceroute results based on the 'destination_ip_responded' field."""
        succeed_count = 0
        fail_count = 0
        for result in results:
            if result['destination_ip_responded']:
                succeed_count += 1
            else:
                fail_count += 1
        return (succeed_count >= SUCCESS_THRESHOLD), succeed_count, fail_count

    @staticmethod
    def eval_ping(results: list):
        """Evaluate ping results based on the 'rcvd' field."""
        succeed_count = 0
        fail_count = 0
        for result in results:
            if result['rcvd'] > 0:
                succeed_count += 1
            else:
                fail_count += 1
        return (succeed_count >= SUCCESS_THRESHOLD), succeed_count, fail_count

    @staticmethod
    def eval_http(results: list):
        """Evaluate HTTP results based on a HTTP 200 response code."""
        succeed_count = 0
        fail_count = 0
        for result in results:
            # Anchor HTTP measurements should only have one result.
            if len(result['result']) > 1:
                logging.warning('HTTP too many results')
                logging.warning(result)
            inner_result = result['result'][0]
            if 'res' in inner_result and inner_result['res'] == 200:
                succeed_count += 1
            else:
                fail_count += 1
        return (succeed_count >= SUCCESS_THRESHOLD), succeed_count, fail_count

    @staticmethod
    def eval_msm(msm_type: str, results: list):
        match msm_type:
            case 'traceroute':
                return AnchorChecker.eval_traceroute(results)
            case 'ping':
                return AnchorChecker.eval_ping(results)
            case 'http':
                return AnchorChecker.eval_http(results)
            case _:
                raise ValueError(f'Invalid measurement type: {msm_type}')

    def run(self):
        logging.info('anchor metadata')
        self.process_anchor_metadata()
        logging.info('anchor measurements')
        self.process_anchor_measurements()
        logging.info('anchor probe metadata')
        self.process_anchor_probe_metadata()
        logging.info('anchor measurement metadata')
        self.process_measurement_metadata()
        logging.info('anchor measurement results')
        self.process_measurement_results()
        logging.info('anchor measurement succeeded')
        self.process_measurement_succeeded()
        logging.info('write data files')
        self.write_data_files()
        logging.info('write stats files')
        self.write_stats_file()


if __name__ == '__main__':
    FORMAT = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(
        format=FORMAT,
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    checker = AnchorChecker()
    checker.run()
    sys.exit(0)
