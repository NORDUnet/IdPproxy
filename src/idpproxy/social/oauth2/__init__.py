import json
from oic.utils.authn.client import CLIENT_AUTHN_METHOD

from idpproxy.social import Social

from oic.oauth2 import rndstr
from oic.oauth2 import Client
from oic.oauth2.message import ErrorResponse
from oic.oauth2.message import AccessTokenResponse
from oic.oauth2.message import AuthorizationResponse
from oic.oauth2.message import AuthorizationRequest

from oic.utils.http_util import Redirect

import logging

logger = logging.getLogger(__name__)


class OAuth2(Social):
    def __init__(self, client_id, client_secret, **kwargs):
        Social.__init__(self, client_id, client_secret, **kwargs)
        self.access_token_response = AccessTokenResponse
        try:
            self._scope = ",".join(self.extra["scope"])
        except KeyError:
            self._scope = ""
        self.token_response_body_type = "urlencoded"

    #noinspection PyUnusedLocal
    def begin(self, environ, server_env, start_response, cookie,
              sid, info):

        state = rndstr()
        server_env["CACHE"].alternate_sid(sid, state)
        callback = server_env["base_url"] + self.social_endpoint

        # redirect the user to facebook for the authentication
        ar = AuthorizationRequest().from_dict({"client_id": self.client_id,
                                               "redirect_uri": callback,
                                               "state": state,
                                               "response_type": ["code"],
                                               "scope": self._scope})
        url = ar.request(self.extra["authorization_endpoint"])
        logger.info("[OAuth2] callback url: %s" % url)
        if cookie:
            resp = Redirect(url, headers=[cookie])
        else:
            resp = Redirect(url)
        return resp(environ, start_response)

    #noinspection PyUnusedLocal
    def userinfo_endpoint(self, tokenresp):
        return self.extra["userinfo_endpoint"]

    #noinspection PyUnusedLocal
    def phaseN(self, environ, info, server_env, sid):
        session = server_env["CACHE"][sid]

        callback = server_env["base_url"] + self.social_endpoint

        client = Client(client_id=self.client_id,
                        client_authn_method=CLIENT_AUTHN_METHOD)
        response = client.parse_response(AuthorizationResponse, info, "dict")
        logger.info("Response: %s" % response)

        if isinstance(response, ErrorResponse):
            logger.info("%s" % response)
            session["authentication"] = "FAILED"
            return False, "Authentication failed or permission not granted"

        req_args = {
            "redirect_uri": callback,
            "client_secret": self.client_secret,
        }

        client.token_endpoint = self.extra["token_endpoint"]
        tokenresp = client.do_access_token_request(
            scope=self._scope,
            body_type=self.token_response_body_type,
            request_args=req_args,
            authn_method="client_secret_post",
            state=response["state"],
            response_cls=self.access_token_response)

        if isinstance(tokenresp, ErrorResponse):
            logger.info("%s" % tokenresp)
            session["authentication"] = "FAILED"
            return False, "Authentication failed or permission not granted"

        # Download the user profile and cache a local instance of the
        # basic profile info
        result = client.fetch_protected_resource(
            self.userinfo_endpoint(tokenresp), token=tokenresp["access_token"])

        logger.info("Userinfo: %s" % result.text)
        profile = json.loads(result.text)

        logger.info("PROFILE: %s" % (profile, ))
        session["service"] = self.name
        session["authentication"] = "OK"
        session["status"] = "SUCCESS"
        session["authn_auth"] = self.authenticating_authority
        session["permanent_id"] = profile["id"]

        server_env["CACHE"][sid] = session

        return True, self.convert(profile), session
