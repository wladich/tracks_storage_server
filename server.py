# coding: utf-8
import hashlib
import base64
import msgpack
import psycopg2
import re
import logging
import uuid
import json

import config
from nktk_raw_pb2 import TrackView


MAX_STORE_SIZE = 1000000


log = logging.getLogger(__name__)
log_level = getattr(logging, config.log['level'])
log.setLevel(log_level)
if not config.log.get('file'):
    log_handler = logging.StreamHandler()
else:
    log_handler = logging.FileHandler(config.log['file'])
log_handler.setLevel(log_level)
log_formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
log_handler.setFormatter(log_formatter)
log.addHandler(log_handler)


_connection = None


def check_connection(conn):
    try:
        conn.cursor().execute('SELECT 1')
        return True
    except psycopg2.OperationalError:
        return False


def get_connection():
    global _connection
    if not _connection or _connection.closed or not check_connection(_connection):
        _connection = psycopg2.connect(**config.db)
        _connection.set_session(autocommit=True)
    return _connection


def insert_geodata(data):
    table_name = 'geodata'
    connection = get_connection()
    data = buffer(data)
    with connection.cursor() as cursor:
        sql = 'INSERT INTO %s (data) VALUES (%%s) ON CONFLICT DO NOTHING RETURNING id' % table_name
        cursor.execute(sql, (data,))
        res = cursor.fetchone()
        if res is None:
            sql = 'SELECT id FROM %s WHERE md5(data)=md5(%%s)' % table_name
            cursor.execute(sql, (data,))
            res = cursor.fetchone()
            assert res
    return res[0]


def insert_trackview(data, data_hash):
    table_name = 'trackview'
    connection = get_connection()
    data = buffer(data)
    with connection.cursor() as cursor:
        sql = 'INSERT INTO %s (data, hash) VALUES (%%s, %%s) ON CONFLICT DO NOTHING RETURNING id' % table_name
        cursor.execute(sql, (data, data_hash))
        res = cursor.fetchone()
        is_new = bool(res)
        if not is_new:
            sql = 'SELECT id FROM %s WHERE hash=%%s' % table_name
            cursor.execute(sql, (data_hash,))
            res = cursor.fetchone()
            assert res
    return {'id': res[0], 'is_new': is_new}


def select_geodata(id_):
    table_name = 'geodata'
    connection = get_connection()
    with connection.cursor() as cursor:
        sql = 'SELECT data FROM %s WHERE id=%%s' % table_name
        cursor.execute(sql, (id_,))
        res = cursor.fetchone()
        if res:
            return str(res[0])


def select_trackview(data_hash):
    table_name = 'trackview'
    connection = get_connection()
    with connection.cursor() as cursor:
        sql = 'SELECT id, data FROM %s WHERE hash=%%s' % table_name
        cursor.execute(sql, (data_hash,))
        res = cursor.fetchone()
        if res:
            return {'id': res[0], 'data': str(res[1])}


def decode_url_safe_base64(s):
    return s.replace('-', '+').replace('_', '/').decode('base64')


def encode_url_safe_base64(s):
    return base64.standard_b64encode(s).replace('+', '-').replace('/', '_')


def parse_trackviews_from_request(s):
    result = []
    for part in s.split('/'):
        if not part:
            continue
        tv = TrackView()
        s = decode_url_safe_base64(part)
        version = ord(s[0]) - 64
        if version != 4:
            raise ValueError('Unknown version %s' % version)
        tv.ParseFromString(s[1:])
        result.append((tv.view, tv.track))
    return result


def offload_geodata(trackviews):
    result = []
    for view_data, track_data in trackviews:
        geodata_id = insert_geodata(track_data)
        result.append((view_data, geodata_id))
    return result


def serialize_trackviews_for_storage(trackviews):
    return msgpack.dumps(trackviews)


def serialize_trackviews_for_response(trackviews):
    version = 4
    version_char = chr(version + 64)
    res = []
    for view_data, geodata in trackviews:
        tv = TrackView()
        tv.view = view_data
        tv.track = geodata
        s = encode_url_safe_base64(version_char + tv.SerializeToString())
        res.append(s)
    return '/'.join(res)


def parse_trackviews_from_storage(s):
    return msgpack.loads(s)


def load_geodata(trackviews):
    result = []
    for view_data, geodata_id in trackviews:
        geodata = select_geodata(geodata_id)
        result.append((view_data, geodata))
    return result


def store_track(tracks, data_hash):
    tracks = offload_geodata(tracks)
    s = serialize_trackviews_for_storage(tracks)
    return insert_trackview(s, data_hash)


def retrieve_track(data_hash):
    res = select_trackview(data_hash)
    if res is None:
        return None
    tracks = parse_trackviews_from_storage(res['data'])
    tracks = load_geodata(tracks)
    s = serialize_trackviews_for_response(tracks)
    return {'id': res['id'], 'track': s}


def encode_hash(s):
    return base64.standard_b64encode(s).replace('/', '_').replace('+', '-').rstrip('=')


def read_log(ip_addr, trackview_id):
    connection = get_connection()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO read_log (ip_addr, time, trackview_id) VALUES (%s, 'now', %s)", (ip_addr, trackview_id))


def write_log(ip_addr, trackview_id):
    connection = get_connection()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO write_log (ip_addr, time, trackview_id) VALUES (%s, 'now', %s)",
            (ip_addr, trackview_id))


class Application(object):
    STATUS_OK = '200 OK'
    STATUS_NOT_FOUND = '404', 'Not Found'
    STATUS_LENGTH_REQUIRED = '411', 'Length Required'
    STATUS_PAYLOAD_TOO_LARGE = '413', 'Payload Too Large'
    STATUS_BAD_REQUEST = '400', 'Bad Request'
    STATUS_INTERNAL_SERVER_ERROR = '500', 'Internal Server Error'

    def __init__(self, environ, start_response):
        self.environ = environ
        self._start_response = start_response
        self.request_id = uuid.uuid4().get_hex()

    def log(self, level, message='', **extra):
        extra = dict(extra, request_id=self.request_id)
        message += ' ' + json.dumps(extra)
        if level == 'EXCEPTION':
            log.exception(message)
        else:
            log.log(getattr(logging, level), message)

    def start_response(self, status, headers):
        headers = headers[:]
        headers.append(('Access-Control-Allow-Origin', '*'))
        self._start_response(status, headers)

    def handle_store_track(self, request_data_hash):
        self.log('INFO', 'Storing track')
        try:
            size = int(self.environ['CONTENT_LENGTH'])
        except (ValueError, KeyError):
            self.log('INFO', 'No content-length')
            return self.error(self.STATUS_LENGTH_REQUIRED)
        if size > MAX_STORE_SIZE:
            self.log('INFO', 'Request content_length too big', max_size=MAX_STORE_SIZE, content_length=size)
            return self.error(self.STATUS_PAYLOAD_TOO_LARGE)
        data = self.environ['wsgi.input'].read(size)
        self.log('INFO', request_body=data)
        if len(data) != size:
            self.log('INFO', 'Request body smaller then content-lenght', content_length=size, body_size=len(data))
            return self.error(self.STATUS_BAD_REQUEST)

        if not data:
            self.log('INFO', 'Request body empty')
            return self.error(self.STATUS_BAD_REQUEST)

        data_hash = encode_hash(hashlib.md5(data).digest())
        if data_hash != request_data_hash:
            self.log('INFO', 'Wrong data hash in request', data_hash=data_hash, request_data_hash=request_data_hash)
            return self.error(self.STATUS_BAD_REQUEST)
        try:
            tracks = parse_trackviews_from_request(data)
        except:
            self.log('EXCEPTION', 'Error parsing track from request')
            return self.error(self.STATUS_BAD_REQUEST)
        try:
            res = store_track(tracks, data_hash)
        except:
            self.log('EXCEPTION', 'Error storing track')
            return self.error(self.STATUS_INTERNAL_SERVER_ERROR)
        if res['is_new']:
            self.log('INFO', 'Stored new track')
        else:
            self.log('INFO', 'Track found in storage')
        self.log('INFO', 'Success storing track')
        try:
            write_log(self.environ.get('REMOTE_ADDR'), res['id'])
        except:
            self.log('EXCEPTION', 'Error writing write-log')
        self.start_response(self.STATUS_OK, [])
        return ['']

    def handle_retrieve_track(self, hash):
        self.log('INFO', 'Retreiving track')
        try:
            res = retrieve_track(hash)
        except:
            self.log('EXCEPTION', 'Error retrieving track')
            return self.error(self.STATUS_INTERNAL_SERVER_ERROR)
        if res is None:
            self.log('INFO', 'Key not found')
            return self.error(self.STATUS_NOT_FOUND)
        self.log('INFO', 'Success retrieving track')
        try:
            read_log(self.environ.get('REMOTE_ADDR'), res['id'])
        except:
            self.log('EXCEPTION', 'Error writing read-log')
        self.start_response(self.STATUS_OK, [])
        return [res['track']]

    def error(self, status):
        self.start_response('%s %s' % status, [])
        return [json.dumps({'requestId': self.request_id, 'status': status[1], 'code': status[0]})]

    def get_headers(self):
        headers = {}
        for k, v in self.environ.iteritems():
            if k.startswith('HTTP_'):
                headers[k[5:]] = v
        return headers

    def route(self):
        try:
            method = self.environ['REQUEST_METHOD']
            uri = self.environ['PATH_INFO']
            self.log('INFO', 'Request accepted', method=method, uri=uri, headers=self.get_headers(),
                     remote_addr=self.environ['REMOTE_ADDR'])
            if method == 'GET':
                m = re.match(r'^/track/([A-Za-z0-9_-]+)$', uri)
                if m:
                    return self.handle_retrieve_track(m.group(1))
            if method == 'POST':
                m = re.match(r'^/track/([A-Za-z0-9_-]+)$', uri)
                if m:
                    return self.handle_store_track(m.group(1))
            self.log('INFO', 'Request did not match any handler')
            return self.error(self.STATUS_NOT_FOUND)
        except Exception:
            try:
                self.log('EXCEPTION')
            except:
                pass
            return self.error(self.STATUS_INTERNAL_SERVER_ERROR)


def application(environ, start_response):
    return Application(environ, start_response).route()


if __name__ == '__main__':
    from wsgiref.simple_server import make_server
    httpd = make_server('localhost', 8080, application)
    httpd.serve_forever()
