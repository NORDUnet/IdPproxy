import cgi

__author__ = 'Hans Hoerberg - Copyright 2013 Umea Universitet'
import re
import os
import xmldsig
import xmlenc
import logging
import uuid
import json
from idpproxy import utils
from saml2.httputil import Response, NotFound
from urlparse import parse_qs
from mako.lookup import TemplateLookup
from jwkest.jwe import JWE_RSA
from saml2.extension import mdattr
from saml2.saml import Attribute
from saml2.saml import AttributeValue
from saml2.mdstore import MetadataStore
from saml2.mdstore import MetaData
from saml2.sigver import get_xmlsec_binary
from saml2 import attribute_converter
from saml2 import saml
from saml2.extension import mdui
from saml2.extension import idpdisc
from saml2.extension import dri
from saml2.extension import ui
from saml2 import md

# The class is responsible for taking care of all requests for generating SP
# metadata for the social services used by the IdPproxy.

_log = logging.getLogger(__name__)


class MetadataGeneration(object):
    #Body
    CONST_BODY = "body"+uuid.uuid4().urn
    #JSON
    CONST_TYPEJSON = "application/json"
    #Base directory for this class.
    CONST_BASE = os.path.dirname(os.path.abspath(__file__))
    #Directory containing all static files.
    CONST_STATIC_FILE = CONST_BASE + "/files/static/"
    #Directory containing all mako files.
    CONST_STATIC_MAKO = CONST_BASE + "/files/mako/"

    #Start path - accessed directly by user.
    CONST_METADATA = "/metadata"
    # Path to show the generated metadata.
    # Next step from the CONST_METADATA path.
    CONST_METADATASAVE = "/metadata/save"
    # Next step from the CONST_METADATASAVE path.
    CONST_METADATAVERIFY = "/metadata/verify"
    # Next step from the CONST_METADATAVERIFY1 path.
    CONST_METADATAVERIFYJSON = "/metadata/verifyjson"

    #Static html file shown when a user tries to access an unknown path under
    # the CONST_METDATA path.
    CONST_UNKNOWFILE = CONST_STATIC_FILE + "unknown.html"
    #Static hthml file that is presented to the user if an unknown error occurs.
    CONST_UNKNOWERROR = CONST_STATIC_FILE + "unknownError.html"

    #Algoritm used to encrypt social service secret and key.
    CONST_ALG = "RSA-OAEP"
    #Encryption method used to encrypt social service secret and key.
    CONST_ENCRYPT = "A256GCM"

    #Needed for reading metadatafiles.
    CONST_ONTS = {
        saml.NAMESPACE: saml,
        mdui.NAMESPACE: mdui,
        mdattr.NAMESPACE: mdattr,
        dri.NAMESPACE: dri,
        ui.NAMESPACE: ui,
        idpdisc.NAMESPACE: idpdisc,
        md.NAMESPACE: md,
        xmldsig.NAMESPACE: xmldsig,
        xmlenc.NAMESPACE: xmlenc
    }
    #Needed for reading metadatafiles.
    CONST_ATTRCONV = attribute_converter.ac_factory("attributemaps")

    def __init__(self, logger, conf, publicKey, privateKey, metadataList):
        """
        Constructor.
        Initiates the class.
        :param logger: Logger to be used when something needs to be logged.
        :param conf: idp_proxy_conf see IdpProxy/conig/idp_proxy_conf.example.py
        :param key: A RSA key to be used for encryption.
        :param metadataList: A list of metadata files.
            [{"local": ["swamid-1.0.xml"]}, {"local": ["sp.xml"]}]
        :raise:
        """
        if (logger is None) or (conf is None) or (publicKey is None)or (privateKey is None):
            raise ValueError(
                "A new instance must include a value for logger, conf and key.")
        #Public key to be used for encryption.
        self.jwe_rsa = JWE_RSA()
        self.publicKey = publicKey
        self.privateKey = privateKey
        #Used for presentation of mako files.
        self.lookup = TemplateLookup(
            directories=[MetadataGeneration.CONST_STATIC_MAKO + 'templates',
                         MetadataGeneration.CONST_STATIC_MAKO + 'htdocs'],
            module_directory='modules',
            input_encoding='utf-8',
            output_encoding='utf-8')
        #The logger.
        self.logger = logger
        #A list of all social services used by this IdPproxy.
        self.socialServiceKeyList = []
        #A list of all service providers used by this sp.
        self.spKeyList = []
        for key in conf:
            self.socialServiceKeyList.append(conf[key]["name"])

        try:
            xmlsec_path = get_xmlsec_binary(["/opt/local/bin"])
        except:
            try:
                xmlsec_path = get_xmlsec_binary(["/usr/local/bin"])
            except:
                self.logger.info('Xmlsec must be installed! Tries /usr/bin/xmlsec1.')
                xmlsec_path = '/usr/bin/xmlsec1'

        self.xmlsec_path = xmlsec_path

        for metadata in metadataList:
            mds = MetadataStore(MetadataGeneration.CONST_ONTS.values(),
                                MetadataGeneration.CONST_ATTRCONV, xmlsec_path,
                                disable_ssl_certificate_validation=True)
            mds.imp(metadata)
            for entityId in mds.keys():
                self.spKeyList.append(entityId)

    def verifyHandleRequest(self, path):
        """
        Verifies if the given path should be handled by this class.
        :param path: A path.
        :return: True if the path should be handled by this class, otherwise false.
        """
        return re.match(MetadataGeneration.CONST_METADATA + ".*", path)

    def getQueryDict(self, environ):
        """
        Retrieves a dictionary with query parameters.
        :param environ: The wsgi enviroment.
        :return: A dictionary with query parameters.
        """
        qs = {}
        query = environ.get("QUERY_STRING", "")
        if not query:
            post_env = environ.copy()
            post_env['QUERY_STRING'] = ''
            query = cgi.FieldStorage(fp=environ['wsgi.input'], environ=post_env,
                                     keep_blank_values=True)
            if query is not None:
                try:
                    for item in query:
                        qs[query[item].name] = query[item].value
                except:
                    qs[MetadataGeneration.CONST_BODY] = query.file.read()

        else:
            qs = dict((k, v if len(v) > 1 else v[0]) for k, v in
                      parse_qs(query).iteritems())

        return qs

    def handleRequest(self, environ, start_response, path):
        """
        Call this method from the wsgi application.
        Handles the request if the path i matched by verifyHandleRequest and any static file or
        CONST_METADATA or CONST_METADATASAVE.
        :param environ: wsgi enviroment
        :param start_response: the start response
        :param path: the requested path
        :return: a response fitted for wsgi application.
        """
        try:
            if path == MetadataGeneration.CONST_METADATA:
                return self.handleMetadata(environ, start_response)
            elif path == MetadataGeneration.CONST_METADATAVERIFY:
                return self.handleMetadataVerify(environ, start_response,
                                               self.getQueryDict(environ))
            elif path == MetadataGeneration.CONST_METADATAVERIFYJSON:
                return self.handleMetadataVerifyJson(environ, start_response,
                                                 self.getQueryDict(environ))
            elif path == MetadataGeneration.CONST_METADATASAVE:
                return self.handleMetadataSave(environ, start_response,
                                               self.getQueryDict(environ))
            else:
                filename = self.CONST_STATIC_FILE + self.getStaticFileName(path)
                if self.verifyStatic(filename):
                    return self.handleStatic(environ, start_response, filename)
                else:
                    return self.handleStatic(environ, start_response,
                                             MetadataGeneration.CONST_UNKNOWFILE)
        except Exception as e:
            self.logger.fatal('Unknown error in handleRequest.', exc_info=True)
            return self.handleStatic(environ, start_response,
                                     MetadataGeneration.CONST_UNKNOWERROR)

    def getStaticFileName(self, path):
        """
        Parses out the static file name from the path.
        :param path: The requested path.
        :return: The static file name.
        """
        if self.verifyHandleRequest(path):
            try:
                return path[len(MetadataGeneration.CONST_METADATA) + 1:]
            except:
                pass
        return ""

    def verifyStatic(self, filename):
        """
        Verifies if a static file exists in the folder IdPproxy/src/idpproxy/metadata/files/static
        :param filename: The name of the file.
        :return: True if the file exists, otherwise false.
        """
        try:
            with open(filename):
                pass
        except IOError:
            return False
        return True

    def handleMetadata(self, environ, start_response):
        """
        Creates the response for the first page in the metadata generation.
        :param environ: wsgi enviroment
        :param start_response: wsgi start respons
        :return: wsgi response for the mako file metadata.mako.
        """
        resp = Response(mako_template="metadata.mako",
                        template_lookup=self.lookup, headers=[])

        argv = {
            "action": MetadataGeneration.CONST_METADATASAVE,
            "sociallist": sorted(self.socialServiceKeyList),
            "spKeyList": sorted(self.spKeyList),
            "verify": MetadataGeneration.CONST_METADATAVERIFY,
        }
        return resp(environ, start_response, **argv)

    def handleMetadataSave(self, environ, start_response, qs):
        """
        Takes the input for the page metadata.mako.
        Encrypts entity id and secret information for the social services.
        Creates the partial xml to be added to the metadata for the service provider.
        :param environ: wsgi enviroment
        :param start_response: wsgi start respons
        :param qs: Query parameters in a dictionary.
        :return: wsgi response for the mako file metadatasave.mako.
        """
        resp = Response(mako_template="metadatasave.mako",
                        template_lookup=self.lookup,
                        headers=[])
        if "entityId" not in qs or "secret" not in qs:
            xml = "Xml could not be generated because no entityId or secret has been sent to the service."
            self.logger.warning(xml)
        else:
            try:
                secretData = '{"entityId": ' + qs["entityId"] + ', "secret":' + qs["secret"] + '}'
                secretDataEncrypted = self.jwe_rsa.encrypt(
                    secretData,
                    {"rsa": [self.publicKey]},
                    MetadataGeneration.CONST_ALG,
                    MetadataGeneration.CONST_ENCRYPT,
                    "public",
                    debug=False)
                val = AttributeValue()
                val.set_text(secretDataEncrypted)
                attr = Attribute(name_format="urn:oasis:names:tc:SAML:2.0:attrname-format:uri",
                                 name="http://social2saml.nordu.net/customer",
                                 attribute_value=[val])
                eattr = mdattr.EntityAttributes(attribute=[attr])
                nspair = {
                    "mdattr": "urn:oasis:names:tc:SAML:metadata:attribute",
                    "samla": "urn:oasis:names:tc:SAML:2.0:assertion"
                }
                xml = eattr.to_string(nspair)
                xmlList = xml.split("\n", 1)

                if len(xmlList) == 2:
                    xml = xmlList[1]

            except Exception as exp:
                self.logger.fatal('Unknown error in handleMetadataSave.',
                                  exc_info=True)
                xml = "Xml could not be generated."
        argv = {
            "home": MetadataGeneration.CONST_METADATA,
            "action": MetadataGeneration.CONST_METADATAVERIFY,
            "xml": xml
        }
        return resp(environ, start_response, **argv)

    def handleMetadataVerify(self, environ, start_response, qs):
        """
        Will show the page for metadata verification (metadataverify.mako).
        :param environ: wsgi enviroment
        :param start_response: wsgi start respons
        :param qs: Query parameters in a dictionary.
        :return: wsgi response for the mako file metadatasave.mako.
        """
        resp = Response(mako_template="metadataverify.mako",
                        template_lookup=self.lookup,
                        headers=[])
        argv = {
            "home": MetadataGeneration.CONST_METADATA,
            "action": MetadataGeneration.CONST_METADATAVERIFYJSON
        }
        return resp(environ, start_response, **argv)

    def handleMetadataVerifyJson(self, environ, start_response, qs):
        """
        Handles JSON metadata verifications.
        The post body must contains a JSON message like { 'xml' : 'a metadata file'}
        :param environ: wsgi enviroment
        :param start_response: wsgi start respons
        :param qs: Query parameters in a dictionary.
        :return: wsgi response contaning a JSON response. The JSON message will contain the parameter ok and services.
                ok will contain true if the metadata file can be parsed, otherwise false.
                services will contain a list of all the service names contained in the metadata file.
        """
        ok = False
        services = "[]"
        try:
            if MetadataGeneration.CONST_BODY in qs:
                jsonMessage = json.loads(qs[MetadataGeneration.CONST_BODY])
                if "xml" in jsonMessage:
                    xml = jsonMessage["xml"]
                    xml = xml.strip()
                    metadataOK = False
                    ci = None
                    try:
                        mds = MetadataStore(MetadataGeneration.CONST_ONTS.values(),
                                            MetadataGeneration.CONST_ATTRCONV, self.xmlsec_path,
                                            disable_ssl_certificate_validation=True)
                        md = MetaData(MetadataGeneration.CONST_ONTS.values(), MetadataGeneration.CONST_ATTRCONV, metadata=xml)
                        md.load()
                        entityId = md.entity.keys()[0]
                        mds.metadata[entityId] = md
                        args = {"metad": mds, "dkeys": {"rsa": [self.privateKey]}}
                        ci = utils.ConsumerInfo(['metadata'], **args)
                        metadataOK = True
                    except:
                        self.logger.info('Could not parse the metadata file in handleMetadataVerifyJSON.',
                                          exc_info=True)
                    services = "["
                    first = True
                    if ci is not None:
                        for item in ci._info:
                            if item._ava is not None and entityId in item._ava:
                                for social in item._ava[entityId]:
                                    if not first:
                                        services += ","
                                    else:
                                        first = False
                                    services += '"' + social + '"'
                    services += "]"
                    if metadataOK:
                        ok = True
        except:
            self.logger.fatal('Unknown error in handleMetadataVerifyJSON.',
                              exc_info=True)
        resp = Response('{"ok":"' + str(ok) + '", "services":' + services + '}', headers=[('Content-Type', MetadataGeneration.CONST_TYPEJSON)])
        return resp(environ, start_response)

    def handleStatic(self, environ, start_response, path):
        """
        Creates a response for a static file.
        :param environ: wsgi enviroment
        :param start_response: wsgi start response
        :param path: the static file and path to the file.
        :return: wsgi response for the static file.
        """
        try:
            text = open(path).read()
            if path.endswith(".ico"):
                resp = Response(text, headers=[('Content-Type', "image/x-icon")])
            elif path.endswith(".html"):
                resp = Response(text, headers=[('Content-Type', 'text/html')])
            elif path.endswith(".txt"):
                resp = Response(text, headers=[('Content-Type', 'text/plain')])
            elif path.endswith(".css"):
                resp = Response(text, headers=[('Content-Type', 'text/css')])
            else:
                resp = Response(text, headers=[('Content-Type', 'text/xml')])
        except IOError:
            resp = NotFound()
        return resp(environ, start_response)