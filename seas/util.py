import re
import os
import sys
import csv
import base64
import logging.config
import urlparse
import threading
import bson
import chardet
import requests
import pkg_resources

from datetime import datetime
from cStringIO import StringIO
from bag import csv2


TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
re_wildcard = re.compile(r'(\{[a-zA-Z][^\}]*\})')
re_newline = re.compile('(\r(?=[^\n]))|\r\n')
manager = pkg_resources.ResourceManager()


def setup_logging(config_file):
    '''Setup logging like pyramid.paster.setup_logging but does
    NOT disable existing loggers
    '''
    path = config_file.split('#')[0]
    full_path = os.path.abspath(path)
    here = os.path.dirname(full_path)
    return logging.config.fileConfig(
        full_path, dict(__file__=full_path, here=here),
        disable_existing_loggers=False)


def strptime(s):
    'Return a datetime object from an iso formatted string'
    if '.' in s:
        s, us = s.split('.')
    else:
        us = '0'
    result = datetime.strptime(s, TIMESTAMP_FORMAT)
    return result.replace(microsecond=int(us))


def pattern_for(path):
    '''Convert a swagger path spec to a url'''
    if not path.startswith('/'):
        path = '/' + path
    parts = re_wildcard.split(path)
    pattern_parts = []
    for part in parts:
        if part.startswith('{'):
            name = part[1:-1]
            pattern_parts.append('(?P<{}>[^/]+)'.format(name))
        else:
            pattern_parts.append(re.escape(part))
    return re.compile(''.join(pattern_parts) + '$')


def actual_response_lines(resp):
    '''requests.Response.iter_lines() is BROKEN. It does not handle \r\n in
    as sane way. This generator does.
    '''
    cur_buf = ''
    for chunk in resp.iter_content(requests.models.ITER_CHUNK_SIZE):
        cur_buf += chunk
        cur_buf = re_newline.sub('\n', cur_buf)
        lines = cur_buf.split('\n')
        for line in lines[:-1]:
            yield line + '\n'
        cur_buf = lines[-1]
    if cur_buf:
        for line in cur_buf.split('\n'):
            yield line + '\n'



def really_unicode(s):
    # Try to guess the encoding
    def encodings():
        yield None
        yield 'utf-8'
        yield chardet.detect(s[:1024])['encoding']
        yield chardet.detect(s)['encoding']
        yield 'latin-1'
    return _attempt_encodings(s, encodings())


def load_content(url):
    '''Versatile text loader. Handles the following url types:

    http[s]://...
    file:///... (or just leave off the file://)
    egg://EggName/path
    '''
    parsed = urlparse.urlparse(url)
    if parsed.scheme == 'file://':
        assert not parsed.netloc, 'File urls must start with file:///'
        return open(parsed.path).read()
    if parsed.scheme == '':
        return open(parsed.path).read()
    elif parsed.scheme in ('http', 'https'):
        return requests.get(url).text
    elif parsed.scheme == 'egg':
        dist = pkg_resources.get_distribution(parsed.netloc)
        fn = dist.get_resource_filename(manager, parsed.path)
        return open(fn).read()
    else:
        assert False, "Don't know how to handle {} URLs".format(parsed.scheme)


def datetime_spec_from_strings(before, after, ts_field='ts', template='%Y-%m-%d'):
    if before and isinstance(before, basestring):
        before = datetime.strptime(before, template)
    if after and isinstance(after, basestring):
        after = datetime.strptime(after, template)
    return {ts_field: {'$gte': after, '$lte': before}}

def jsonify(obj, **json_kwargs):
    if isinstance(obj, bson.ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat() + 'Z'   #strftime(TIMESTAMP_FORMAT)
    elif hasattr(obj, '__json__'):
        return jsonify(obj.__json__(**json_kwargs))
    elif isinstance(obj, dict):
        return dict((k, jsonify(v)) for k, v in obj.items())
    elif isinstance(obj, list):
        return map(jsonify, obj)
    return obj


def chunk(iterator, chunksize):
    chunk = []
    iterator = iter(iterator)
    while True:
        try:
            chunk.append(iterator.next())
        except StopIteration:
            if chunk:
                yield chunk
            break
        if len(chunk) >= chunksize:
            yield chunk
            chunk = []


def csv_from_row_iter(it):
    sio = StringIO()
    wr = csv2.CsvWriter(sio)
    for row in it:
        sio.seek(0)
        sio.truncate()
        wr.put(row)
        yield sio.getvalue()


def nonce(size=48, encoding='base64url'):
    raw = os.urandom(size)
    if encoding == 'base64url':
        return base64.urlsafe_b64encode(raw).strip()
    else:
        return raw.encode(encoding).strip()

class Retrying(object):
    '''Class that retries its __call__() method n times on a given set of exceptions'''

    def __init__(self, retries, *exception_classes):
        self.retries = retries
        self.exception_classes = exception_classes

    def __call__(self, func, *args, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                return func(*args, **kwargs)
            except self.exception_classes as err:
                info = sys.exc_info()
        raise info[0], info[1], info[2]


def _attempt_encodings(s, encodings):
    if s is None:
        return u''
    for enc in encodings:
        try:
            if enc is None:
                return unicode(s)  # try default encoding
            else:
                return unicode(s, enc)
        except (UnicodeDecodeError, LookupError):
            pass
    # Return the repr of the str -- should always be safe
    return unicode(repr(str(s)))[1:-1]


class BetterJoiningThread(threading.Thread):

    def __init__(self, *args, **kwargs):
        self.result = None
        self.exc_info = None
        super(BetterJoiningThread, self).__init__(*args, **kwargs)

    def run(self):
        try:
            self.result = super(BetterJoiningThread, self).run()
        except Exception:
            self.exc_info = sys.exc_info()

    def join(self):
        super(BetterJoiningThread, self).join()
        if self.exc_info is not None:
            raise self.exc_info[0], self.exc_info[1], self.exc_info[2]
        return self.result
