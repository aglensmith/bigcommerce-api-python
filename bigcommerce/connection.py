"""
Connection Module

Handles put and get operations to the Bigcommerce REST API
"""
import base64
import hashlib
import hmac

try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode

import logging
import requests
import jwt

from json import dumps, loads
from math import ceil
from time import sleep

from bigcommerce.exception import *

log = logging.getLogger("bigcommerce.connection")


class Connection(object):
    """
    Connection class manages the connection to the Bigcommerce REST API.
    """

    def __init__(self, host, auth, api_path='/api/{}/{}'):
        self.host = host
        self.api_path = api_path
        self.timeout = 7.0  # need to catch timeout?

        log.info("API Host: %s/%s" % (self.host, self.api_path))

        # set up the session
        self._session = requests.Session()
        self._session.auth = auth
        self._session.headers = {"Accept": "application/json"}

        self._last_response = None  # for debugging

    def full_path(self, url, version='v2'):
        if '/api/{}/{}' in self.api_path:
            return "https://" + self.host + self.api_path.format(version, url)
        else:
            return "https://" + self.host + self.api_path.format(url)


    def _run_method(self, method, url, data=None, query=None, headers=None, version='v2'):
        if query is None:
            query = {}
        if headers is None:
            headers = {}

        # make full path if not given
        if url and url[:4] != "http":
            if url[0] == '/':  # can call with /resource if you want
                url = url[1:]
            url = self.full_path(url, version)
        elif not url:  # blank path
            url = self.full_path(url, version)

        qs = urlencode(query)
        if qs:
            qs = "?" + qs
        url += qs

        # mess with content
        if data:
            if not headers:  # assume JSON
                data = dumps(data)
                headers = {'Content-Type': 'application/json'}
            if headers and 'Content-Type' not in headers:
                data = dumps(data)
                headers['Content-Type'] = 'application/json'
        log.debug("%s %s" % (method, url))
        # make and send the request
        return self._session.request(method, url, data=data, timeout=self.timeout, headers=headers)

    # CRUD methods

    def get(self, resource="", rid=None, **query):
        """
        Retrieves the resource with given id 'rid', or all resources of given type.
        Keep in mind that the API returns a list for any query that doesn't specify an ID, even when applying
        a limit=1 filter.
        Also be aware that float values tend to come back as strings ("2.0000" instead of 2.0)

        Keyword arguments can be parsed for filtering the query, for example:
            connection.get('products', limit=3, min_price=10.5)
        (see Bigcommerce resource documentation).
        """
        if rid:
            if resource[-1] != '/':
                resource += '/'
            resource += str(rid)
        response = self._run_method('GET', resource, query=query)
        return self._handle_response(resource, response)

    def update(self, resource, rid, updates):
        """
        Updates the resource with id 'rid' with the given updates dictionary.
        """
        if resource[-1] != '/':
            resource += '/'
        resource += str(rid)
        return self.put(resource, data=updates)

    def create(self, resource, data):
        """
        Create a resource with given data dictionary.
        """
        return self.post(resource, data)

    def delete(self, resource, rid=None):  # note that rid can't be 0 - problem?
        """
        Deletes the resource with given id 'rid', or all resources of given type if rid is not supplied.
        """
        if rid:
            if resource[-1] != '/':
                resource += '/'
            resource += str(rid)
        response = self._run_method('DELETE', resource)
        return self._handle_response(resource, response, suppress_empty=True)

    # Raw-er stuff

    def make_request(self, method, url, data=None, params=None, headers=None, version='v2'):
        response = self._run_method(method, url, data, params, headers, version=version)
        return self._handle_response(url, response)

    def put(self, url, data):
        """
        Make a PUT request to save data.
        data should be a dictionary.
        """
        response = self._run_method('PUT', url, data=data)
        log.debug("OUTPUT: %s" % response.content)
        return self._handle_response(url, response)

    def post(self, url, data, headers={}):
        """
        POST request for creating new objects.
        data should be a dictionary.
        """
        response = self._run_method('POST', url, data=data, headers=headers)
        return self._handle_response(url, response)

    def _handle_response(self, url, res, suppress_empty=True):
        """
        Returns parsed JSON or raises an exception appropriately.
        """
        self._last_response = res
        result = {}
        if res.status_code in (200, 201, 202):
            try:
                result = res.json()
            except Exception as e:  # json might be invalid, or store might be down
                e.message += " (_handle_response failed to decode JSON: " + str(res.content) + ")"
                raise  # TODO better exception
        elif res.status_code == 204 and not suppress_empty:
            raise EmptyResponseWarning("%d %s @ %s: %s" % (res.status_code, res.reason, url, res.content), res)
        elif res.status_code >= 500:
            raise ServerException("%d %s @ %s: %s" % (res.status_code, res.reason, url, res.content), res)
        elif res.status_code == 429:
            raise RateLimitingException("%d %s @ %s: %s" % (res.status_code, res.reason, url, res.content), res)
        elif res.status_code >= 400:
            raise ClientRequestException("%d %s @ %s: %s" % (res.status_code, res.reason, url, res.content), res)
        elif res.status_code >= 300:
            raise RedirectionException("%d %s @ %s: %s" % (res.status_code, res.reason, url, res.content), res)

        if 'data' in result: # for v3
            result = result['data']
        return result

    def __repr__(self):
        return "%s %s%s" % (self.__class__.__name__, self.host, self.api_path)


class OAuthConnection(Connection):
    """
    Class for making OAuth requests on the Bigcommerce v2 API

    Providing a value for access_token allows immediate access to resources within registered scope.
    Otherwise, you may use fetch_token with the code, context, and scope passed to your application's callback url
    to retrieve an access token.

    The verify_payload method is also provided for authenticating signed payloads passed to an application's load url.
    """

    def __init__(self, client_id=None, store_hash=None, access_token=None, host='api.bigcommerce.com',
                 api_path='/stores/{}/{}/{}', rate_limiting_management=None):
        self.client_id = client_id
        self.store_hash = store_hash
        self.host = host
        self.api_path = api_path
        self.timeout = 7.0  # can attach to session?
        self.rate_limiting_management = rate_limiting_management

        self._session = requests.Session()
        self._session.headers = {"Accept": "application/json",
                                 "Accept-Encoding": "gzip"}
        if access_token and store_hash:
            self._session.headers.update(self._oauth_headers(client_id, access_token))

        self._last_response = None  # for debugging

        self.rate_limit = {}

    def full_path(self, url, version='v2'):
        if '/api/{}/{}/{}' in self.api_path:
            return "https://" + self.host + self.api_path.format(self.store_hash, version, url)
        return "https://" + self.host + self.api_path.format(self.store_hash, version, url)

    @staticmethod
    def _oauth_headers(cid, atoken):
        return {'X-Auth-Client': cid,
                'X-Auth-Token': atoken}

    @staticmethod
    def verify_payload(signed_payload, client_secret):
        """
        Given a signed payload (usually passed as parameter in a GET request to the app's load URL) and a client secret,
        authenticates the payload and returns the user's data, or False on fail.

        Uses constant-time str comparison to prevent vulnerability to timing attacks.
        """
        encoded_json, encoded_hmac = signed_payload.split('.')
        dc_json = base64.b64decode(encoded_json)
        signature = base64.b64decode(encoded_hmac)
        expected_sig = hmac.new(client_secret.encode(), base64.b64decode(encoded_json), hashlib.sha256).hexdigest()
        authorised = hmac.compare_digest(signature, expected_sig.encode())
        return loads(dc_json.decode()) if authorised else False

    @staticmethod
    def verify_payload_jwt(signed_payload, client_secret, client_id):
        """
        Given a signed payload JWT (usually passed as parameter in a GET request to the app's load URL)
        and a client secret, authenticates the payload and returns the user's data, or error on fail.
        """
        return jwt.decode(signed_payload,
                          client_secret,
                          algorithms=["HS256"],
                          audience=client_id,
                          options={
                            'verify_iss': False
                          })

    def fetch_token(self, client_secret, code, context, scope, redirect_uri,
                    token_url='https://login.bigcommerce.com/oauth2/token'):
        """
        Fetches a token from given token_url, using given parameters, and sets up session headers for
        future requests.
        redirect_uri should be the same as your callback URL.
        code, context, and scope should be passed as parameters to your callback URL on app installation.

        Raises HttpException on failure (same as Connection methods).
        """
        res = self.post(token_url, {'client_id': self.client_id,
                                    'client_secret': client_secret,
                                    'code': code,
                                    'context': context,
                                    'scope': scope,
                                    'grant_type': 'authorization_code',
                                    'redirect_uri': redirect_uri},
                        headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self._session.headers.update(self._oauth_headers(self.client_id, res['access_token']))
        return res

    def _handle_response(self, url, res, suppress_empty=True):
        """
        Adds rate limiting information on to the response object
        """
        result = Connection._handle_response(self, url, res, suppress_empty)
        if 'X-Rate-Limit-Time-Reset-Ms' in res.headers:
            self.rate_limit = dict(ms_until_reset=int(res.headers['X-Rate-Limit-Time-Reset-Ms']),
                                   window_size_ms=int(res.headers['X-Rate-Limit-Time-Window-Ms']),
                                   requests_remaining=int(res.headers['X-Rate-Limit-Requests-Left']),
                                   requests_quota=int(res.headers['X-Rate-Limit-Requests-Quota']))
            if self.rate_limiting_management:
                if self.rate_limiting_management['min_requests_remaining'] >= self.rate_limit['requests_remaining']:
                    if self.rate_limiting_management['wait']:
                        sleep(ceil(float(self.rate_limit['ms_until_reset']) / 1000))
                    if self.rate_limiting_management.get('callback_function'):
                        callback = self.rate_limiting_management['callback_function']
                        args_dict = self.rate_limiting_management.get('callback_args')
                        if args_dict:
                            callback(args_dict)
                        else:
                            callback()

        return result