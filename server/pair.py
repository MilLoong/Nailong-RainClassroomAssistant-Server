"""Generate a short-lived pairing QR from the administrator's machine."""
import argparse
import json
import os
from urllib.parse import urlparse

import httpx
import qrcode
import qrcode.image.svg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url', help='Public HTTPS server origin')
    parser.add_argument('--output', default='pairing.svg')
    args = parser.parse_args()
    origin = args.url.rstrip('/')
    parsed = urlparse(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        parser.error('use an HTTPS origin without credentials, query or fragment')
    response = httpx.post(origin + '/v1/admin/pairings',
                          headers={'Authorization': 'Bearer ' + os.environ['ADMIN_TOKEN']}, timeout=15)
    response.raise_for_status()
    pairing = response.json()
    payload = json.dumps({'version': 1, 'server': origin, 'code': pairing['code']})
    qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage).save(args.output)
    print(f'Pairing QR saved to {args.output}; expires in 120 seconds. Treat it as a secret.')


if __name__ == '__main__':
    main()
