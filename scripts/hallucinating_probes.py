import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests


def main():
    r = requests.get('https://atlas.ripe.net/api/v2/measurements/40072013/latest')
    r.raise_for_status()
    results = r.json()

    hallucating_probes = [e['prb_id'] for e in results if e['destination_ip_responded']]

    data_output_dir = 'data/hallucinating-probes'
    date = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    data_output_file = os.path.join(data_output_dir, date.strftime('%Y%m%d') + '.hallucinating_probes.csv')
    os.makedirs(data_output_dir, exist_ok=True)
    with open(data_output_file, 'w') as f:
        f.write('prb_id\n')
        f.write('\n'.join(map(str, sorted(hallucating_probes))) + '\n')

    stats_output_file = 'stats/hallucinating-probes.csv'
    stats = {'date': date, 'hallucinating_probes': len(hallucating_probes)}
    if not os.path.exists(stats_output_file):
        stats_df = pd.DataFrame(stats, index=[0])
    else:
        stats_df = pd.read_csv(stats_output_file)
        stats_df.loc[-1] = stats
    stats_df.to_csv(stats_output_file, index=False)


if __name__ == '__main__':
    FORMAT = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(
        format=FORMAT,
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    main()
    sys.exit(0)
