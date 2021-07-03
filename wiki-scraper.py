#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
from mwclient import Site
import json
import os
import platform
import plistlib
import re
import remotezip
import shutil
import subprocess
import sys
import tempfile
import time


class BetaScraper(object):
    def __init__(self, site):
        self.site = site
        self.api = dict()

    def grab_pages(self, filters):
        pages = list()
        for result in self.site.search('Beta Firmware/'):
            if any(x in result['title'] for x in filters) and '.x' in result['title']:
                pages.append(result['title'])

        return pages

    def grab_urls(self, pages):
        urls = list()
        for page in pages:
            page_regex = re.findall('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', self.site.pages[page].text())
            [urls.append(url) for url in page_regex if url.endswith('.ipsw') and url not in urls]

        return urls

    def scrape_firm(self, url):
        if any(domain in url for domain in ('developer.apple.com', 'adcdownload.apple.com')): # Inaccessible domains
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                with remotezip.RemoteZip(url) as ipsw:
                    manifest = next(f for f in ipsw.namelist() if 'Manifest' in f)
                    ipsw.extract(manifest, tmpdir)
            except:
                return

            with open('/'.join((tmpdir, manifest)), 'rb') as f:
                bm = plistlib.load(f)

            args = (
                'tsschecker',
                '-B',
                bm['BuildIdentities'][0]['Info']['DeviceClass'],
                '-m',
                f'{tmpdir}/{manifest}'
            )

            tsschecker = subprocess.run(args, stdout=subprocess.PIPE, universal_newlines=True)

        for device in bm['SupportedProductTypes']:
            if device not in self.api.keys():
                self.api[device] = list()

            self.api[device].append({
                'version': bm['ProductVersion'],
                'buildid': bm['ProductBuildVersion'],
                'url': url,
                'signed': True if 'IS being signed!' in tsschecker.stdout else False
            })

    def write_api(self, path):
        if os.path.exists(path):
            shutil.rmtree(path)

        os.mkdir(path)
        for device in self.api.keys():
            with open(f'{path}/{device}', 'w') as f:
                json.dump(sorted(self.api[device], key=lambda firm: firm['version'], reverse=True), f)

def main():
    if platform.system() == 'Windows':
        sys.exit('[ERROR] Windows is not supported. Exiting.')

    if shutil.which('tsschecker') is None:
        sys.exit('[ERROR] tsschecker is not installed. Exiting.')

    start_time = time.time()
    device_types = ('Apple TV', 'iPad', 'iPad Air', 'iPad Pro', 'iPad Mini', 'iPhone', 'iPod touch')
    scraper = BetaScraper(Site('www.theiphonewiki.com'))

    print('[1] Scraping The iPhone Wiki...')
    pages = scraper.grab_pages(device_types)

    print('[2] Scraping beta IPSW URLs...')
    urls = scraper.grab_urls(pages)

    print('[3] Grabbing rest of info from IPSWs (this will take a while, please wait)...')
    with ThreadPoolExecutor() as executor:
        for url in urls:
            executor.submit(scraper.scrape_firm, url)

    print('[4] Writing out API...')
    scraper.write_api('betas')

    print(f'Done! Took {round(time.time() - start_time)}s.')

if __name__ == "__main__":
    main()
