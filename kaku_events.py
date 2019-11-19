#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:copyright: (c) 2013-2016 by Mike Taylor
:license: CC0 1.0 Universal, see LICENSE for more details.
"""

import os
import json
import uuid
import types
import errno
import shutil
import logging
import datetime
import argparse

import pytz
import redis
import jinja2
import ronkyuu
import requests
import markdown2

from bs4 import BeautifulSoup
from logging.handlers import RotatingFileHandler
from dateutil.parser import parse
from bearlib.config import Config, findConfigFile
from bearlib.tools import normalizeFilename

try:
    # python 3
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse


logger = logging.getLogger(__name__)

def getTimestamp():
    utcdate   = datetime.datetime.utcnow()
    tzLocal   = pytz.timezone('America/New_York')
    return tzLocal.localize(utcdate, is_dst=None)

def mkpath(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def createPath(path, log):
    result = True
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        log.exception(exc)
        if os.path.isdir(path):
            pass
        else:
            result = False
    return result

def escXML(text, escape_quotes=False):
    if isinstance(text, types.UnicodeType):
        s = list(text)
    else:
        if isinstance(text, types.IntType):
            s = str(text)
        else:
            s = text
        s = list(unicode(s, 'utf-8', 'ignore'))

    cc      = 0
    matches = ('&', '<', '"', '>')

    for c in s:
        if c in matches:
            if c == '&':
                s[cc] = u'&amp;'
            elif c == '<':
                s[cc] = u'&lt;'
            elif c == '>':
                s[cc] = u'&gt;'
            elif escape_quotes:
                s[cc] = u'&quot;'
        cc += 1
    return ''.join(s)

def readMD(targetFile):
    result  = {}
    content = []
    header  = True
    mdFile  = '%s.md' % targetFile
    for line in open(mdFile, 'r').readlines():
        item = line.decode('utf-8', 'xmlcharrefreplace')
        if header and len(item.strip()) == 0:
            header = False
        if header and ':' in item:
            tag, value          = item.split(':', 1)
            result[tag.lower()] = value.strip()
        else:
            content.append(item)
    result['modified'] = os.path.getmtime(mdFile)
    result['path']     = os.path.dirname(mdFile)
    result['content']  = u''.join(content[1:])
    if 'created' not in result and 'date' in result:
        result['created'] = result['date']
    if 'published' not in result and 'created' in result:
        result['published'] = result['created']
    return result

def writeMD(targetFile, data):
    page = mdPost % data
    with open('%s.md' % targetFile, 'w+') as h:
        h.write(page.encode('utf-8'))

def loadMetadata(targetFile):
    mdData = readMD(targetFile)
    if os.path.exists('%s.json' % targetFile):
        with open('%s.json' % targetFile, 'r') as h:
            result = json.load(h)
        if 'published' not in result:
            result['published'] = result['created']
        if 'route' not in result:
            result['route'] = u'%(year)s/%(doy)s/%(slug)s' % result
        if 'url' not in result:
            result['url']   = '%s%s.html' % (cfg.baseroute, result['route'])
        for key in ('created', 'published', 'updated', 'deleted'):
            if key in result:
                result[key] = parse(result[key])
    else:
        for key in ('created', 'published'):
            mdData[key] = parse(mdData[key])
        created         = mdData['created']
        mdData['key']   = created.strftime('%Y%m%d%H%M%S')
        mdData['year']  = created.strftime('%Y')
        mdData['doy']   = created.strftime('%j')
        mdData['route'] = u'%(year)s/%(doy)s/%(slug)s' % mdData
        mdData['url']   = '%s%s.html' % (cfg.baseroute, mdData['route'])
        result          = {}
        for key in mdData:
            result[key] = mdData[key]
    result['modified'] = mdData['modified']
    result['content']  = mdData['content']
    return result

def saveMetadata(targetFile, data):
    if 'created' not in data:
        data['created'] = data['date']
    if 'published' not in data:
        data['published'] = data['created']
    for key in ('created', 'published', 'updated', 'deleted'):
        if key in data:
            data[key] = data[key].strftime('%Y-%m-%d %H:%M:%S')
    with open('%s.json' % targetFile, 'w+') as h:
        h.write(json.dumps(data, indent=2))

def loadOurWebmentions(targetFile):
    result = {}
    if os.path.exists('%s.mentions' % targetFile):
        with open('%s.mentions' % targetFile, 'r') as h:
            result = json.load(h)
    return result

def saveOurMentions(targetFile, mentions):
    logger.info('saving webmentions for %s' % targetFile)
    with open('%s.mentions' % targetFile, 'w+') as h:
        h.write(json.dumps(mentions, indent=2))

def scanOurMentions(sourceURL, mentions):
    # loop thru to see if this mention is already present
    found = None
    for key in mentions:
        item = mentions[key]['mention']
        url  = urlparse(item['sourceURL'])
        if url.netloc == sourceURL.netloc and url.path == sourceURL.path:
            found = key
            break
    logger.info('scanOurMentions result [%s]' % found)
    return found

def loadOutboundWebmentions(targetFile):
    result = {}
    if os.path.exists('%s.outboundmentions' % targetFile):
        with open('%s.outboundmentions' % targetFile, 'r') as h:
            result = json.load(h)
    return result

def saveOutboundWebmentions(targetFile, mentions):
    logger.info('saving outbound webmentions from %s' % targetFile)
    with open('%s.outboundmentions' % targetFile, 'w+') as h:
        h.write(json.dumps(mentions, indent=2))

def checkOutboundWebmentions(sourceURL, html, targetFile, update=False):
    logger.info('checking for outbound webmentions [%s]' % sourceURL)
    try:
        cached   = loadOutboundWebmentions(targetFile)
        found    = ronkyuu.findMentions(sourceURL, content=html)
        mentions = {}

        # loop thru webmentions found in our post and
        # check if they are new/updated or already seen
        for href in found['refs']:
            if sourceURL != href:
                logger.info(href)
                key     = 'webmention::%s::%s' % (sourceURL, href)
                keySeen = db.exists(key)
                if keySeen:
                    if update:
                        keySeen = False
                        s       = 'update forced'
                    else:
                        s = 'already processed'
                else:
                    s = 'new mention'
                logger.info('\t%s [%s]' % (s, key))
                mentions[key] = { 'key':     key,
                                  'href':   href,
                                  'keySeen': keySeen,
                                  'removed': False
                                }
        # loop thru found webmentions and check against cache for any removed
        for key in cached:
            if key not in mentions:
                mentions[key] = cached[key]
                mentions[key]['removed'] = True
                if 'keySeen' not in mentions[key]:
                    mentions[key]['keySeen'] = False
        removed = []
        for key in mentions:
            mention = mentions[key]
            logger.info('seen: %(keySeen)s removed: %(removed)s [%(key)s]' % mention)

            # send webmentions for new/updated or removed
            if mention['removed'] or not mention['keySeen']:
                if mention['removed']:
                    removed.append(key)

                href = mention['href']
                wmStatus, wmUrl, debug = ronkyuu.discoverEndpoint(href, test_urls=False, debug=True)
                logger.info('webmention endpoint discovery: %s [%s]' % (wmStatus, wmUrl))

                if len(debug) > 0:
                    logger.info('\n\tdebug: '.join(debug))
                if wmUrl is not None and wmStatus == 200:
                    logger.info('\tfound webmention endpoint %s for %s' % (wmUrl, href))
                    resp, debug = ronkyuu.sendWebmention(sourceURL, href, wmUrl, debug=True)
                    if len(debug) > 0:
                        logger.info('\n\tdebug: '.join(debug))
                    if resp.status_code == requests.codes.ok:
                        if key not in cached:
                            cached[key] = { 'key':    key,
                                            'href':   href,
                                            'wmUrl':  wmUrl,
                                            'status': resp.status_code
                                          }
                        if len(resp.history) == 0:
                            db.set(key, resp.status_code)
                            logger.info('\twebmention sent successfully')
                        else:
                            logger.info('\twebmention POST was redirected')
                    else:
                        logger.info('\twebmention send returned a status code of %s' % resp.status_code)
        for key in removed:
            del cached[key]
            db.delete(key)

        saveOutboundWebmentions(targetFile, cached)
    except:
        logger.exception('exception during checkOutboundWebmentions')

def postUpdate(targetFile, action=None):
    """Generate data for targeted file.

    All mentions to the post are checked for updates.
    The post is also scanned for any outbound Webmentions.

    targetFile: path and filename without extension.
    """
    pageEnv          = {}
    templateLoader   = jinja2.FileSystemLoader(searchpath=cfg.paths.templates)
    templates        = jinja2.Environment(loader=templateLoader)
    postTemplate     = templates.get_template(cfg.templates['post'])
    postPageTemplate = templates.get_template(cfg.templates['postPage'])
    post             = loadMetadata(targetFile)
    ourMentions      = loadOurWebmentions(targetFile)

    # bring over site config items
    for s in ('title',):
        pageEnv[s] = cfg[s]

    if action == 'update':
        post['updated'] = getTimestamp()

    if os.path.exists('%s.deleted' % targetFile):
        logger.info('post [%s] is marked as deleted' % targetFile)
        if action == 'delete' and 'deleted' not in post:
            post['deleted'] = getTimestamp()
        post['html']        = '<p>This article has been deleted.</p>'
        pageEnv['title']    = 'This article has been deleted'
        pageEnv['meta']     = '<meta http-equiv="Status" content="410 GONE" />'
        pageEnv['mentions'] = []
    else:
        logger.info('updating post [%s]' % targetFile)
        post['html'] = md.convert(post['content'])
        if 'deleted' in post:
            del post['deleted']
        removed = []
        for key in ourMentions:
            m = ourMentions[key]['mention']
            r = requests.get(m['sourceURL'], verify=True)

            if r.status_code == 410:
                logger.info('a mention no longer exists - removing [%s]' % key)
                removed.append(key)
            else:
                if 'charset' in r.headers.get('content-type', ''):
                    content = r.text
                else:
                    content = r.content
                soup   = BeautifulSoup(content, 'html5lib')
                status = None
                for meta in soup.findAll('meta', attrs={'http-equiv': lambda x: x and x.lower() == 'status'}):
                    try:
                        status = int(meta['content'].split(' ')[0])
                    except:
                        pass
                if status == 410:
                    logger.info('a mention no longer exists (via http-equiv) - removing [%s]' % key)
                    removed.append(key)
        for key in removed:
            del ourMentions[key]
        mentions = []
        for key in ourMentions:
            m = ourMentions[key]['mention']
            # convert string dates into datetime's for template processing
            if 'postDate' in m:
                m['postDate'] = parse(m['postDate'])
            mentions.append(m)
        pageEnv['title']    = post['title']
        pageEnv['mentions'] = mentions
        pageEnv['meta']     = metaEmbed % post

    post['xml']     = escXML(post['html'])
    pageEnv['post'] = post
    postHtml        = postTemplate.render(pageEnv)
    postPage        = postPageTemplate.render(pageEnv)

    with open('%s.html' % targetFile, 'w+') as h:
        h.write(postHtml.encode('utf-8'))

    htmlDir = os.path.join(cfg.paths.output, post['year'], post['doy'])
    if not os.path.exists(htmlDir):
        mkpath(htmlDir)
    with open(os.path.join(htmlDir, '%s.html' % post['slug']), 'w+') as h:
        h.write(postPage.encode('utf-8'))

    saveMetadata(targetFile, post)
    checkOutboundWebmentions('%s%s' % (cfg.baseurl, post['url']), postHtml, targetFile, update=True)

def checkPost(targetFile, eventData):
    """Check if the post's markdown file is present and create it if not.

    targetFile: path and filename without extension.
    eventData:  Micropub data to create the post from.
    """
    if not os.path.exists('%s.md' % targetFile):
        if 'micropub' in eventData:
            micropub = eventData['micropub']
            if 'content' in micropub:
                content = micropub['content']
            if 'html' in micropub and len(micropub['html']) > 0:
                content.append(micropub['html'])
            if 'category' in micropub:
                categories = ','.join(micropub['category'])
            else:
                categories = ''
            if 'photo' in micropub:
                for url, alt in micropub['photo']:
                    if len(alt) > 0:
                        t = ' alt="%s"' % alt
                    else:
                        t = ''
                    content.append('<img src="%s"%s></img>' % (url, t))
            logger.info(micropub.keys())
            if 'photo_files' in micropub:
                for filename in micropub['photo_files']:
                    photoSrc = os.path.join(cfg.paths.uploads, filename)
                    photoTgt = os.path.join(cfg.paths.output, 'images', filename)
                    logger.info('photo file: %s %s %s' % (filename, photoSrc, photoTgt))
                    shutil.copyfile(photoSrc, photoTgt)
                    url = '%s%s%s/%s' % (cfg.baseurl, cfg.baseroute, 'images', filename)
                    content.append('<img src="%s"></img>' % url)

            # location    = "geo:40.0958,-74.90736;u=92"
            # in-reply-to = "https://bear.im/bearlog/2016/123/testing-delete.html"
            # bookmark-of = "https://bear.im"
            # category    = [u'code', u'indieweb']
            # html        = [u'<p>something</p><p class="">line2</p><p class="">line3<br></p><p class=""><br></p>']
            logger.info('content: %d %s' % (len(content), content))

            data = { 'created':   eventData['timestamp'],
                     'published': eventData['timestamp'],
                     'slug':      eventData['slug'],
                     'author':    'bear',
                     'tags':      categories,
                     'content':   '\n'.join(content),
                     'title':     eventData['title'],
                     'summary':   eventData['title'],
                     'year':      eventData['year'],
                     'doy':       eventData['doy'],
                     'uuid':      str(uuid.uuid4()),
                     'payload':   eventData['micropub'],
                   }
            writeMD(targetFile, data)
        else:
            logger.error('checkPost for [%s] - no Micropub data included' % targetFile)

def mentionDelete(mention):
    logger.info('mention delete of [%s] within [%s]' % (mention['targetURL'], mention['sourceURL']))
    # update() handles removal of out of date mentions
    targetURL   = urlparse(mention['targetURL'])
    targetRoute = targetURL.path.replace(cfg.baseroute, '')
    postUpdate(os.path.join(cfg.paths.content, targetRoute))

def mentionUpdate(mention):
    logger.info('mention update of [%s] within [%s]' % (mention['targetURL'], mention['sourceURL']))

    targetPath = urlparse(mention['targetURL'].strip()).path
    pathItems  = targetPath.split('.')
    logger.info('[%s] %s' % (targetPath, pathItems))
    if pathItems[-1].lower() == 'html':
        targetPath = '.'.join(pathItems[:-1])

    eventDate   = getTimestamp()
    sourceURL   = urlparse(mention['sourceURL'])
    targetRoute = targetPath.replace(cfg.baseroute, '')
    targetFile  = os.path.join(cfg.paths.content, targetRoute)

    logger.info('targetFile [%s]' % targetFile)

    ourMentions = loadOurWebmentions(targetFile)
    found       = scanOurMentions(sourceURL, ourMentions)

    if found is not None:
        logger.info('updated mention of [%s] within [%s]' % (found, mention['targetURL']))
        ourMentions[found]['updated'] = eventDate.strftime('%Y-%m-%dT%H:%M:%S')
        ourMentions[found]['mention'] = mention
    else:
        key              = 'mention::%s::%s' % (sourceURL.netloc, sourceURL.path)
        ourMentions[key] = { 'created': mention['postDate'],
                             'updated': None,
                             'mention': mention,
                           }
        logger.info('added mention of [%s] within [%s]' % (key, mention['targetURL']))

    saveOurMentions(targetFile, ourMentions)
    postUpdate(targetFile)

def indexUpdate():
    """Scan all posts and generate the index page.
    """
    frontpage = {}
    logger.info('building index page')
    for path, dirlist, filelist in os.walk(cfg.paths.content):
        if len(filelist) > 0:
            for item in filelist:
                filename, ext = os.path.splitext(item)
                if ext in ('.json',) and '.mentions.json' not in item:
                    if os.path.exists(os.path.join(path, '%s.deleted' % filename)):
                        logger.info('skipping deleted post [%s]' % filename)
                    else:
                        page = loadMetadata(os.path.join(path, filename))
                        frontpage[page['key']] = page
    templateLoader = jinja2.FileSystemLoader(searchpath=cfg.paths.templates)
    templates      = jinja2.Environment(loader=templateLoader)
    indexTemplate  = templates.get_template(cfg.templates['index'])
    pageEnv        = { 'posts': [],
                       'title': cfg.title,
                     }
    frontpageKeys = frontpage.keys()
    frontpageKeys.sort(reverse=True)

    for key in frontpageKeys[:cfg.index_articles]:
        pageEnv['posts'].append(frontpage[key])

    page     = indexTemplate.render(pageEnv)
    indexDir = os.path.join(cfg.paths.output)

    if not os.path.exists(indexDir):
        mkpath(indexDir)
    with open(os.path.join(indexDir, 'index.html'), 'w+') as h:
        h.write(page.encode('utf-8'))

def isUpdated(path, filename, force=False):
    mFile = os.path.join(path, '%s.md' % filename)
    jFile = os.path.join(path, '%s.json' % filename)
    if os.path.exists(os.path.join(path, '%s.deleted' % filename)):
        return 'delete'
    if os.path.exists(jFile):
        mTime = os.path.getmtime(mFile)
        jTime = os.path.getmtime(jFile)
        if force or mTime > jTime:
            return 'update'
        else:
            return 'unchanged'
    else:
        return 'create'

def gather(filepath, filename=None, force=False):
    logger.info('gather [%s] [%s] [%s]' % (filepath, filename, force))
    if filename is None:
        if filepath is None:
            logger.error('A specific file or a path to walk must be specified')
        else:
            for path, dirlist, filelist in os.walk(filepath):
                if len(filelist) > 0:
                    for item in filelist:
                        filename, ext = os.path.splitext(item)
                        if ext in ('.md',):
                            state = isUpdated(path, filename, force)
                            key   = 'kaku-event::%s::%s::%s' % ('post', state, str(uuid.uuid4()))
                            data  = { 'type':   'post',
                                      'action': state,
                                      'data':   { 'path': path,
                                                  'file': filename
                                                },
                                      'key':    key
                                    }
                            db.set(key, json.dumps(data))
                            db.publish(cfg.events, key)
    else:
        s = normalizeFilename(filename)
        if not os.path.exists(s):
            s = normalizeFilename(os.path.join(filepath, filename))
        logger.info('checking [%s]' % s)
        if os.path.exists(s):
            path          = os.path.dirname(s)
            filename, ext = os.path.splitext(s)
            if ext in ('.md',):
                state = isUpdated(path, filename, force)
                key   = 'kaku-event::%s::%s::%s' % ('post', state, str(uuid.uuid4()))
                data  = { 'type':   'post',
                          'action': state,
                          'data':   { 'path': path,
                                      'file': filename
                                    },
                          'key':    key
                        }
                db.set(key, json.dumps(data))
                db.publish(cfg.events, key)

def handlePost(eventAction, eventData):
    """Process the Kaku event for Posts.

    eventAction: create, update, delete, undelete or unchanged
    eventData:   a dict that contains information about the post

    Micropub generated post events will have eventData keys:
        slug, title, location, timestamp, micropub

    Post events generated by the gather daemon will have keys:
        path, file
    """
    if eventAction == 'create':
        if 'path' in eventData:
            postDir    = eventData['path']
            targetFile = eventData['file']
        else:
            timestamp         = parse(eventData['timestamp'])
            eventData['year'] = str(timestamp.year)
            eventData['doy']  = timestamp.strftime('%j')
            slug       = eventData['slug']
            postDir    = os.path.join(cfg.paths.content, eventData['year'], eventData['doy'])
            targetFile = os.path.join(postDir, slug)
            if not os.path.exists(postDir):
                mkpath(postDir)
        checkPost(targetFile, eventData)
        postUpdate(targetFile, eventAction)

    elif eventAction in ('update', 'delete', 'add'):
        if 'file' in eventData:
            targetFile = eventData['file']
        else:
            targetURL   = urlparse(eventData['url'])
            targetRoute = targetURL.path.replace(cfg.baseroute, '')
            targetFile  = os.path.join(cfg.paths.content, targetRoute)
        changed = False
        if eventAction == 'delete':
            with open('%s.deleted' % targetFile, 'a'):
                os.utime('%s.deleted' % targetFile, None)
            changed = True
        elif eventAction == 'update':
            actionData = eventData['micropub']
            actionKey  = eventData['actionkey']
            data       = loadMetadata(targetFile)
            for key in ('slug', 'tags', 'content', 'html'):
                logger.info(' -- %s: %s' % (key, data[key]))
            logger.info('update (%s) %s' % (actionKey, json.dumps(actionData)))
            if actionKey == 'replace':
                if 'content' in actionData:
                    data['content'] = '\n'.join(actionData['content'])
                    changed         = True
                if 'category' in actionData:
                    tags = data['tags'].split(',')
                    for tag in actionData['category']:
                        if tag not in tags:
                            tags.append(tag)
                            changed = True
                    data['tags'] = ','.join(tags)
            elif actionKey == 'add':
                if 'content' in actionData:
                   data['content'] += '\n'.join(actionData['content'])
                   changed          = True
                if 'category' in actionData:
                    tags = data['tags'].split(',')
                    for tag in actionData['category']:
                        if tag not in tags:
                            tags.append(tag)
                            changed = True
                    data['tags'] = ','.join(tags)
            elif actionKey == 'delete':
                if 'category' in actionData:
                    if type(actionData) is dict:
                        tags = data['tags'].split(',')
                        for tag in actionData['category']:
                            if tag in tags:
                                tags.remove(tag)
                                changed = True
                        data['tags'] = ','.join(tags)
                    else:
                        data['tags'] = ''
                        changed      = True
            for key in ('slug', 'tags', 'content', 'html'):
                logger.info(' -- %s: %s' % (key, data[key]))
            if changed:
                writeMD(targetFile, data)
                saveMetadata(targetFile, data)

        if changed:
            postUpdate(targetFile, eventAction)

    elif eventAction == 'undelete':
        logger.info(eventData.keys())
        if 'url' in eventData:
            targetURL   = urlparse(eventData['url'])
            targetRoute = targetURL.path.replace(cfg.baseroute, '')
            targetFile  = os.path.join(cfg.paths.content, targetRoute)
            logger.info('checking delete marker %s.deleted' % targetFile)
            if os.path.exists('%s.deleted' % targetFile):
                logger.info('removing delete marker')
                os.remove('%s.deleted' % targetFile)
                postUpdate(targetFile, eventAction)
    indexUpdate()

def handleMentions(eventAction, eventData):
    """Process the Kaku event for mentions.

    eventAction: create, update or delete
    eventData:   dict with the keys sourceURL, targetURL,
                 vouchDomain, vouched, postDate, hcard, mf2data
    """
    if eventAction == 'create' or eventAction == 'update':
        mentionUpdate(eventData)
    elif eventAction == 'delete':
        mentionDelete(eventData)

def handleGather(eventData):
    if 'file' in eventData:
        gather(cfg.paths.content, eventData['file'], eventData['force'])
    else:
        gather(cfg.paths.content)

def handleEvent(eventKey):
    """Process an incoming Kaku Event.

    Retrieve the event data from the key given and call the appropriate handler.

    Valid Event Types are mention, post, gather

    For gather events, only the data item will be found
    For mention and post, action and data will be found

    Valid Event Action are create, update, delete, undelete
    Event Data is a dict of items relevant to the event
    """
    try:
        event     = json.loads(db.get(eventKey))
        eventType = event['type']

        if eventType == 'gather':
            handleGather(event['data'])
        else:
            eventAction = event['action']
            eventData   = event['data']
            logger.info('dispatching %(action)s for %(type)s' % event)
            if eventType == 'post':
                handlePost(eventAction, eventData)
            elif eventType == 'mention':
                handleMentions(eventAction, eventData)
        db.expire(eventKey, 86400)
    except:
        logger.exception('error during event [%s]' % eventKey)

def initLogging(logpath, logname):
    logFormatter = logging.Formatter("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    logfilename  = os.path.join(logpath, logname)
    logHandler   = RotatingFileHandler(logfilename, maxBytes=1024 * 1024 * 100, backupCount=7)
    logHandler.setFormatter(logFormatter)
    logger.addHandler(logHandler)
    logger.setLevel(logging.DEBUG)

def getRedis(redisURL):
    url  = urlparse(redisURL)
    host = url.netloc
    if ':' in host:
        host, port = host.split(':')
        port       = int(port)
    else:
        port = 6379
    if len(url.path) > 0:
        db = int(url.path[1:])
    else:
        db = 0
    return redis.StrictRedis(host=host, port=port, db=db)

# Example config file
# {
#     "baseroute":  "/bearlog/",
#     "baseurl":    "https://bear.im",
#     "index_articles": 15,
#     "redis": "redis://127.0.0.1:6379/1",
#     "markdown_extras": [ "fenced-code-blocks", "cuddled-lists" ],
#     "logname": "kaku_events.log",
#     "events": "kaku-events",
#     "paths": {
#         "templates": "/home/bearim/templates/",
#         "content":   "/home/bearim/content/",
#         "output":    "/srv/bear.im/bearlog/",
#         "log":       "/home/bearim/"
#     },
#     "templates": {
#         "post":     "article.jinja",
#         "mention":  "mention.jinja",
#         "postPage": "article_page.jinja",
#         "index":    "blog_index.jinja",
#         "markdown": "post.md",
#         "embed":    "meta.embed"
#     }
# }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='./kaku_events.cfg')
    parser.add_argument('--file',   default=None,
                        help='A specific markdown file to check and then exit')
    parser.add_argument('--force',  default=False, action='store_true',
                        help='Force any found markdown files (or specific file) to be considered an update.')

    args     = parser.parse_args()
    cfgFiles = findConfigFile(args.config)
    cfg      = Config()
    cfg.fromJson(cfgFiles[0])

    initLogging(cfg.paths.log, cfg.logname)
    logger.info('kaku_events started')

    db = getRedis(cfg.redis)

    with open(os.path.join(cfg.paths.templates, cfg.templates.markdown)) as h:
        mdPost = h.read()
    with open(os.path.join(cfg.paths.templates, cfg.templates.embed)) as h:
        metaEmbed = h.read()

    if args.file is not None:
        gather(cfg.paths.content, args.file, args.force)
    else:
        md = markdown2.Markdown(extras=cfg.markdown_extras)
        p  = db.pubsub()

        p.subscribe(cfg.events)
        logger.info('listening for events')
        for item in p.listen():
            if item['type'] == 'message':
                key = item['data']
                if key.startswith('kaku-event::'):
                    logger.info('handling event [%s]' % key)
                    handleEvent(key)
