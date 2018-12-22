import importlib
import warnings
from itertools import chain

from django.core.exceptions import ImproperlyConfigured
from django.utils import six
from django.utils.encoding import force_text, python_2_unicode_compatible
from django.utils.six.moves.urllib.parse import parse_qs, urlparse

from redis import ConnectionError
from redis._compat import unicode
from redis.connection import SSLConnection

from redis_cache.connection import get_connection_identifier_from_client
from redis_cache.connection import pool


def get_servers(location):
    """Returns a list of servers given the server argument passed in from
    Django.
    """
    if isinstance(location, six.string_types):
        servers = location.split(',')
    elif hasattr(location, '__iter__'):
        servers = location
    else:
        raise ImproperlyConfigured(
            '"server" must be an iterable or string'
        )
    return servers


def import_class(path):
    module_name, class_name = path.rsplit('.', 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        raise ImproperlyConfigured('Could not find module "%s"' % module_name)
    else:
        try:
            return getattr(module, class_name)
        except AttributeError:
            raise ImproperlyConfigured('Cannot import "%s"' % class_name)


def parse_connection_kwargs(server, db=None, **kwargs):
    """
    Return a connection pool configured from the given URL.

    For example::

        redis://[:password]@localhost:6379/0
        rediss://[:password]@localhost:6379/0
        unix://[:password]@/path/to/socket.sock?db=0

    Three URL schemes are supported:
        redis:// creates a normal TCP socket connection
        rediss:// creates a SSL wrapped TCP socket connection
        unix:// creates a Unix Domain Socket connection

    There are several ways to specify a database number. The parse function
    will return the first specified option:
        1. A ``db`` querystring option, e.g. redis://localhost?db=0
        2. If using the redis:// scheme, the path argument of the url, e.g.
           redis://localhost/0
        3. The ``db`` argument to this function.

    If none of these options are specified, db=0 is used.

    Any additional querystring arguments and keyword arguments will be
    passed along to the ConnectionPool class's initializer. In the case
    of conflicting arguments, querystring arguments always win.

    NOTE: taken from `redis.ConnectionPool.from_url` in redis-py
    """
    kwargs['unix_socket_path'] = ''
    if '://' in server:
        url = server
        url_string = url
        url = urlparse(url)
        qs = ''

        # in python2.6, custom URL schemes don't recognize querystring values
        # they're left as part of the url.path.
        if '?' in url.path and not url.query:
            # chop the querystring including the ? off the end of the url
            # and reparse it.
            qs = url.path.split('?', 1)[1]
            url = urlparse(url_string[:-(len(qs) + 1)])
        else:
            qs = url.query

        url_options = {}

        for name, value in parse_qs(qs).items():
            if value and len(value) > 0:
                url_options[name] = value[0]

        # We only support redis:// and unix:// schemes.
        if url.scheme == 'unix':
            url_options.update({
                'password': url.password,
                'unix_socket_path': url.path,
            })

        else:
            url_options.update({
                'host': url.hostname,
                'port': int(url.port or 6379),
                'password': url.password,
            })

            # If there's a path argument, use it as the db argument if a
            # querystring value wasn't specified
            if 'db' not in url_options and url.path:
                try:
                    url_options['db'] = int(url.path.replace('/', ''))
                except (AttributeError, ValueError):
                    pass

            if url.scheme == 'rediss':
                url_options['connection_class'] = SSLConnection

        # last shot at the db value
        url_options['db'] = int(url_options.get('db', db or 0))

        # update the arguments from the URL values
        kwargs.update(url_options)

        # backwards compatability
        if 'charset' in kwargs:
            warnings.warn(DeprecationWarning(
                '"charset" is deprecated. Use "encoding" instead'))
            kwargs['encoding'] = kwargs.pop('charset')
        if 'errors' in kwargs:
            warnings.warn(DeprecationWarning(
                '"errors" is deprecated. Use "encoding_errors" instead'))
            kwargs['encoding_errors'] = kwargs.pop('errors')
    else:
        unix_socket_path = None
        if ':' in server:
            host, port = server.rsplit(':', 1)
            try:
                port = int(port)
            except (ValueError, TypeError):
                raise ImproperlyConfigured(
                    "{0} from {1} must be an integer".format(
                        repr(port),
                        server
                    )
                )
        else:
            host, port = None, None
            unix_socket_path = server

        kwargs.update(
            host=host,
            port=port,
            unix_socket_path=unix_socket_path,
            db=db,
        )

    return kwargs


class MethodProxy(object):
    max_call_count = 3

    def __init__(self, client_proxy, client, method_attr):
        self.client_proxy = client_proxy
        self.client = client
        self.method_attr = method_attr

    def _call(self, call_count, *args, **kwargs):
        try:
            return getattr(self.client, self.method_attr)(*args, **kwargs)
        except ConnectionError as exc:
            if call_count == self.max_call_count:
                raise exc

            # Delete connection within our pool.
            connection_identifier = get_connection_identifier_from_client(self.client)
            try:
                del pool._connection_pools[connection_identifier]
            except KeyError:
                pass

            # Recreate the connection
            self.client = self.client_proxy.client = self.client_proxy.create_client()
            self._call(call_count + 1, *args, **kwargs)

    def __call__(self, *args, **kwargs):
        return self._call(1, *args, **kwargs)


class ClientProxy(object):
    client_class = None
    connection_kwargs = None
    connection_pool_kwargs = None
    client = None
    _method_cache = None

    def __init__(self, client_class, connection_kwargs, connection_pool_kwargs):
        self.client_class = client_class
        self.connection_kwargs = connection_kwargs
        self.connection_pool_kwargs = connection_pool_kwargs
        self.client = self.create_client()
        self._method_cache = {}

    def create_client(self):
        client = self.client_class(**self.connection_kwargs)
        complete_kwargs = dict(
            chain(self.connection_kwargs.items(), self.connection_pool_kwargs.items())
        )
        connection_pool = pool.get_connection_pool(client, **complete_kwargs)
        client.connection_pool = connection_pool
        return client

    def __getattr__(self, attr):
        if callable(getattr(self.client, attr)):
            if attr not in self.__dict__['_method_cache']:
                self._method_cache[attr] = MethodProxy(self, self.client, attr)
            return self._method_cache[attr]
        else:
            return getattr(self.client, attr)
