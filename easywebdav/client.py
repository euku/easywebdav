import requests
import platform
from numbers import Number
import xml.etree.cElementTree as xml
from collections import namedtuple

py_majversion, py_minversion, py_revversion = platform.python_version_tuple()

if py_majversion == '2':
    from httplib import responses as HTTP_CODES
    from urlparse import urlparse
    from urllib import quote 
    basestring = basestring
else:
    from http.client import responses as HTTP_CODES
    from urllib.parse import urlparse, quote
    basestring = (str,bytes)

DOWNLOAD_CHUNK_SIZE_BYTES = 1 * 1024 * 1024

class WebdavException(Exception):
    pass

class ConnectionFailed(WebdavException):
    pass


def codestr(code):
    return HTTP_CODES.get(code, 'UNKNOWN')


File = namedtuple('File', ['name', 'size', 'mtime', 'ctime', 'contenttype'])


def prop(elem, name, default=None):
    child = elem.find('.//{DAV:}' + name)
    return default if child is None or not child.text else child.text

def getrealcontenttype(elem):
    resource_type = elem.find('.//{DAV:}resourcetype')
    if resource_type is not None:
        coll = resource_type.find('.//{DAV:}collection')
        if coll is not None:
            return "httpd/unix-directory"
    return prop(elem, 'getcontenttype', '')


def elem2file(elem):
    return File(
        prop(elem, 'href'),
        int(prop(elem, 'getcontentlength', 0)),
        prop(elem, 'getlastmodified', ''),
        prop(elem, 'creationdate', ''),
        getrealcontenttype(elem),
    )


class OperationFailed(WebdavException):
    _OPERATIONS = dict(
        HEAD = "get header",
        GET = "download",
        PUT = "upload",
        DELETE = "delete",
        MKCOL = "create directory",
        PROPFIND = "list directory",
        MOVE = "move file",
        )

    def __init__(self, method, path, expected_code, actual_code):
        self.method = method
        self.path = path
        self.expected_code = expected_code
        self.actual_code = actual_code
        operation_name = self._OPERATIONS[method]
        self.reason = 'Failed to {operation_name} "{path}"'.format(**locals())
        expected_codes = (expected_code,) if isinstance(expected_code, Number) else expected_code
        expected_codes_str = ", ".join('{0} {1}'.format(code, codestr(code)) for code in expected_codes)
        actual_code_str = codestr(actual_code)
        msg = '''\
{self.reason}.
  Operation     :  {method} {path}
  Expected code :  {expected_codes_str}
  Actual code   :  {actual_code} {actual_code_str}'''.format(**locals())
        super(OperationFailed, self).__init__(msg)

class Client(object):
    def __init__(self, host, port=0, auth=None, username=None, password=None,
                 protocol='http', verify_ssl=True, path=None, cert=None):
        if not port:
            port = 443 if protocol == 'https' else 80
        self.baseurl = '{0}://{1}:{2}'.format(protocol, host, port)
        if path:
            self.baseurl = '{0}/{1}'.format(self.baseurl, path)
        self.cwd = '/'
        self.session = requests.session()
        self.session.verify = verify_ssl
        self.session.stream = True

        if cert:
            self.session.cert = cert

        if auth:
            self.session.auth = auth
        elif username and password:
            self.session.auth = (username, password)

    def _send(self, method, path, expected_code, **kwargs):
        url = self._get_url(path)
        response = self.session.request(method, url, allow_redirects=False, **kwargs)
        if isinstance(expected_code, Number) and response.status_code != expected_code \
            or not isinstance(expected_code, Number) and response.status_code not in expected_code:
            raise OperationFailed(method, path, expected_code, response.status_code)
        return response

    def _get_url(self, path):
        path = quote(str(path).strip())
        if path.startswith('/'):
            return self.baseurl + path
        return "".join((self.baseurl, self.cwd, path))

    def cd(self, path):
        path = path.strip()
        if not path:
            return
        stripped_path = '/'.join(part for part in path.split('/') if part) + '/'
        if stripped_path == '/':
            self.cwd = stripped_path
        elif path.startswith('/'):
            self.cwd = '/' + stripped_path
        else:
            self.cwd += stripped_path

    def mkdir(self, path, safe=False):
        expected_codes = 201 if not safe else (201, 301, 405)
        self._send('MKCOL', path, expected_codes)

    def mkdirs(self, path):
        dirs = [d for d in path.split('/') if d]
        if not dirs:
            return
        if path.startswith('/'):
            dirs[0] = '/' + dirs[0]
        old_cwd = self.cwd
        try:
            for dir in dirs:
                try:
                    self.mkdir(dir, safe=True)
                except Exception as e:
                    if e.actual_code == 409:
                        raise
                finally:
                    self.cd(dir)
        finally:
            self.cd(old_cwd)

    def rmdir(self, path, safe=False):
        path = str(path).rstrip('/') + '/'
        expected_codes = 204 if not safe else (204, 404)
        self._send('DELETE', path, expected_codes)

    def delete(self, path):
        self._send('DELETE', path, 204)

    def move(self, path, new_path):
        self._send('MOVE', path, 204,headers={"Destination":new_path,'Connection':'TE','TE':'trailers','Overwrite':'T'})

    def upload(self, local_path_or_fileobj, remote_path, headers=None):
        if isinstance(local_path_or_fileobj, basestring):
            with open(local_path_or_fileobj, 'rb') as f:
                self._upload(f, remote_path, headers)
        else:
            self._upload(local_path_or_fileobj, remote_path, headers)

    def _upload(self, fileobj, remote_path, headers):
        self._send('PUT', remote_path, (200, 201, 204), data=fileobj, headers=headers)

    def download(self, remote_path, local_path_or_fileobj, callback = None):
        response = self._send('GET', remote_path, 200, stream=True)
        if isinstance(local_path_or_fileobj, basestring):
            with open(local_path_or_fileobj, 'wb') as f:
                self._download(f, response, callback)
        else:
            self._download(local_path_or_fileobj, response, callback)

    def _download(self, fileobj, response, callback = None):
        for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE_BYTES):
            fileobj.write(chunk)
            if not callback is None:
                callback(len(chunk))


    def ls(self, remote_path='.'):
        headers = {'Depth': '1'}
        response = self._send('PROPFIND', remote_path, (207, 301), headers=headers)

        # Redirect
        if response.status_code == 301:
            url = urlparse(response.headers['location'])
            return self.ls(url.path)

        tree = xml.fromstring(response.content)
        return [elem2file(elem) for elem in tree.findall('{DAV:}response')]

    def exists(self, remote_path):
        response = self._send('HEAD', remote_path, (200, 301, 404))
        return True if response.status_code != 404 else False
