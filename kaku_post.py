#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:copyright: (c) 2016 by Mike Taylor
:license: CC0 1.0 Universal, see LICENSE for more details.
"""

import os
import sys
import logging
import argparse

import requests
import webbrowser
from bearlib.tools import normalizeFilename

try:
    # python 3
    from urllib.parse import ParseResult, urlparse
except ImportError:
    from urlparse import ParseResult, urlparse


logger = logging.getLogger(__name__)
_post  = """Summary: %(summary)s
Tags: %(tags)s
Url: %(url)s

%(content)s
"""

def writeMD(targetFile, data):
    if 'tags' not in data:
        data['tags'] = ''
    with open(targetFile, 'w') as h:
        h.write(_post % data)

def readMD(targetFile):
    result  = {}
    content = []
    header  = True
    with open(targetFile, 'r') as h:
        for line in h.readlines():
            item = line.decode('utf-8', 'xmlcharrefreplace')
            if header and len(item.strip()) == 0:
                header = False
            if header and ':' in item:
                tag, value          = item.split(':', 1)
                result[tag.lower()] = value.strip()
            else:
                content.append(item)
        result['content'] = u''.join(content[1:])
    return result

def getAccessToken(domainUrl, accessEndpoint):
    url = urlparse(accessEndpoint)
    if len(url.scheme) == 0 or (url.netloc) == 0:
        access = ParseResult(domainUrl.scheme, domainUrl.netloc, accessEndpoint, '', '', '').geturl()
    else:
        access = ParseResult(url.scheme, url.netloc, url.path, '', '', '').geturl()

    logger.info('A web browser will be opened to the %s url so you can retrieve the authentication token which will be needed next.' % access)
    webbrowser.open(accessEndpoint)
    authToken = input('\nEnter your authentication token (remember to use "s around it)? ')

    return authToken

def verifyToken(domain, authtoken):
    r = requests.get('%s/token' % domain, headers={'Authorization': 'Bearer %s' % authToken})
    if r.status_code != requests.codes.ok:
        logger.error('The authentication token validation step failed with status code of %s' % r.status_code)

    return r.status_code == requests.codes.ok


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--domain', default='https://bear.im',
                        help='The domain to recieve our micropub actions.')
    parser.add_argument('--access', default='/access',
                        help='The domain path to request an authentication token from.')
    parser.add_argument('--token', default=None,
                        help='The file to retrieve the authentication token to use for our micropub actions.')
    parser.add_argument('--publish',  default=None,
                        help='Markdown file to publish.')

    args = parser.parse_args()

    logHandler   = logging.StreamHandler()
    logFormatter = logging.Formatter("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    logHandler.setFormatter(logFormatter)
    logger.addHandler(logHandler)
    logger.setLevel(logging.DEBUG)
    logger.info('micropub started')

    domain    = None
    authToken = None
    newToken  = False

    if args.publish is None:
        logger.error('A file to publish must be specified using the --publish command line option.')
        sys.exit(2)

    publish = normalizeFilename(args.publish)

    if not os.path.exists(publish):
        logger.error('Unable to locate the file %s to publish.' % publish)
        sys.exit(2)

    if args.domain is not None:
        url = urlparse(args.domain)
        if len(url.scheme) == 0 or (url.netloc) == 0:
            logger.error('The domain must be specified with a scheme or location.')
            sys.exit(2)
        else:
            domain = ParseResult(url.scheme, url.netloc, url.path, '', '', '').geturl()

    if domain is None:
        logger.error('A domain must be specified using the --domain command line option.')
        sys.exit(2)

    if args.token is None:
        tokenFile = normalizeFilename(os.path.join('~', '.%s.micropub' % url.netloc))
    else:
        tokenFile = normalizeFilename(args.token)

    if os.path.exists(tokenFile):
        with open(tokenFile, 'r') as h:
            try:
                authToken = h.read().strip()
            except BaseException:
                authToken = None
        if authToken is None or len(authToken) == 0:
            logger.error('The authentication token found in %s appears to be empty.' % tokenFile)
            sys.exit(2)

    if authToken is None:
        logger.info('No Authorization token was found or retrieved at %s' % tokenFile)
        if args.access is None:
            logger.error('An access URL is required and must be specified using the --access comamnd line parameter.')
            sys.exit(2)
        else:
            authToken = getAccessToken(url, args.access)
            newToken  = True

    if authToken is None:
        logger.error('No authorization token was presented either via the command line with --token or generated by browsing to the url given at --domain and --access')
        sys.exit(2)
    else:
        if verifyToken(args.domain, authToken):
            newToken = True
        else:
            authToken = getAccessToken(url, args.access)

            if authToken is not None:
                if verifyToken(args.domain, args.access):
                    newToken = True
                else:
                    sys.exit(2)
            else:
                sys.exit(2)

    post = readMD(publish)

    if 'url' in post:
        data = { 'action':  'update',
                 'url':     post['url'],
                 'replace': {
                      'content': [ post['content'] ],
                 }
               }
    else:
        data = { 'type': 'h-entry',
                 'properties': {
                      'summary': [ post['summary'] ],
                      'content': [ post['content'] ],
                 }
               }
        if 'tags' in post:
            data['properties']['category'] = post['tags'].split(',')
    r = requests.post('%s/micropub' % args.domain,
                      json=data,
                      headers={ 'Authorization': 'Bearer %s' % authToken })
    if r.status_code == requests.codes.ok or r.status_code == 202:
        logger.info('Micropub POST returned %s' % r.status_code)
        logger.info(r.text)

        post['url'] = r.headers['Location']
        writeMD(publish, post)
    else:
        logger.error('Error during publish: status code %s' % r.status_code)
        logger.error(r.text)

    if newToken:
        with open(tokenFile, 'w') as h:
            h.write(authToken)
        logger.info('authToken stored in %s' % tokenFile)
