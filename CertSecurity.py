import os
import sys
import tempfile
import requests
import json
import base64
from OpenSSL import crypto


class CertSecurityError(Exception):
    def __init__(self, *args, **kwargs):
        super(CertSecurityError, self).__init__(*args, **kwargs)


class CertSecurityGlobals:
    SSL_DIR = ""
    DEBUG_API = "https://apitest.startssl.com"
    PRODUCTION_API = "https://api.startssl.com"
    DEBUG_MODE = True


# Write a file in each format
def write_all_formats(base_file_name: str, include_text: bool = False):
    """Get all the file names for the different formats for writing to.

    :param base_file_name: The base file name from which the extensions should be added.
    :param include_text: Specifies if a plain-text file should be written.
    :return: Returns the file format and an object for the next file to write
    """

    # Create the array of file types and the extensions
    base_array = [(crypto.FILETYPE_PEM, ".pem"), (crypto.FILETYPE_ASN1, ".der")]
    if include_text:
        base_array.append((crypto.FILETYPE_TEXT, ".txt"))

    # Create each file and return it along with its format
    for file_format, ext in base_array:
        file_name = base_file_name + ext

        # Catch if the file already exists
        if os.path.exists(file_name):
            err_text = "Certificate file exists, aborting. %s" % file_name
            raise OSError(os.errno.EEXIST, err_text)

        # Open file and return it
        with open(file_name, "wb") as f:
            yield file_format, f


# Generate private key
def generate_key(key_file: str = "", key_type=crypto.TYPE_RSA, bits: int = 2048, password: str = None):
    """Generate a private key.

    :param key_file: The path relative to SSL_DIR where the new key is saved. Use an empty string to disable saving.
    :param key_type: Either crypto.TYPE_RSA or crypto.TYPE_DSA. Specifies the type of key to create.
    :param bits: The number of bits to be used in the key.
    :param password: Either a string or a function callback to get password. Set to None if no password.
    :return: The crypto key object generated.
    """

    # Generate key
    key = crypto.PKey()
    key.generate_key(key_type, bits)

    # Only save key if specified
    if key_file != "":
        key_file = os.path.join(CertSecurityGlobals.SSL_DIR, key_file)  # Create full path to the key file

        # Write each file type
        for file_format, file_object in write_all_formats(key_file):
            file_object.write(crypto.dump_privatekey(file_format, key,
                                                     cipher="aes-256-cbc" if password else None,
                                                     passphrase=password))
    return key


# Generate CSR with key pair
def generate_csr(key_pair: crypto.PKey, csr_data: dict, csr_file: str = ""):
    """Generate a CSR with the specified key and data. Fill in missing data from user input.

    :param key_pair: The key pair used to sign the CSR.
    :param csr_data: The dict supplying the data for the CSR.
    :param csr_file: The path relative to SSL_DIR where the CSR is saved. Pass "" to not save the CSR.
    :return: Returns the completed CSR.
    """

    # Ensure a domain name was supplied
    if not csr_data or not csr_data.get('domainName'):
        raise CertSecurityError("Domain name not supplied to generate_csr!")

    # Generate defaults
    sans = [] if not csr_data.get('sans') else csr_data['sans']
    country_name = ""

    # Get CSR data
    domain_name = csr_data.get('domainName')
    while len(country_name) != 2:
        country_name = \
            csr_data.get('countryName') or input('Country Name (2 letter code) [US]: ') or "US"
        print("Invalid country code!") if len(country_name) != 2 else None
    state_or_province_name = \
        csr_data.get('stateOrProvinceName') or input('State or Province Name (full name) [New York]: ') or "New York"
    locality_name = \
        csr_data.get('localityName') or input("Locality Name (eg, city) [New York]: ") or "New York"
    organization_name = \
        csr_data.get('organizationName') or input("Organization Name (eg, company) [ ]: ") or " "
    organizational_unit_name = \
        csr_data.get('organizationalUnitName') or input("Organizational Unit Name (eg, section) [ ]: ") or " "
    email_address = \
        csr_data.get('emailAddress') or input('Email Address [ ]: ') or " "

    # Appends SAN to have 'DNS:'
    ss = []
    for i in sans:
        ss.append("DNS: %s" % i)
    ss = ", ".join(ss)

    # Create CSR request
    req = crypto.X509Req()
    req_subject = req.get_subject()
    req_subject.CN = domain_name
    req_subject.countryName = country_name
    req_subject.stateOrProvinceName = state_or_province_name
    req_subject.localityName = locality_name
    req_subject.organizationName = organization_name
    req_subject.organizationalUnitName = organizational_unit_name
    req_subject.emailAddress = email_address

    # Create extensions
    base_constraints = ([
        crypto.X509Extension(b"keyUsage", False, b"Digital Signature, Key Encipherment"),
        crypto.X509Extension(b"basicConstraints", False, b"CA:FALSE"),
        crypto.X509Extension(b"extendedKeyUsage", False, b"serverAuth,clientAuth")
    ])
    x509_extensions = base_constraints

    # If there are SAN entries, append the base_constraints to include them.
    if ss:
        san_constraint = crypto.X509Extension(b"subjectAltName", False, ss.encode())
        x509_extensions.append(san_constraint)

    # Add extensions to request
    req.add_extensions(x509_extensions)

    # Sign request
    req.set_pubkey(key_pair)
    req.sign(key_pair, "sha256WithRSAEncryption")

    # Save CSR if requested
    if csr_file != "":
        csr_file = os.path.join(CertSecurityGlobals.SSL_DIR, csr_file)

        # Write each file type
        for file_format, file_object in write_all_formats(csr_file):
            file_object.write(crypto.dump_certificate_request(file_format, req))

    return req


# Password retrieval function
def get_password(*_):
    """Get password from user."""

    password = ""

    # Get password
    while True:
        password = input("Please enter a password to encrypt the private key with: ")
        verify = input("Please re-enter password: ")
        if password == verify:
            break
        else:
            print("Passwords do not match!")

    return password.encode()


# StartSSL
def request_certificate(csr: crypto.X509Req, token_id: str, client_cert: crypto.PKCS12, domains: [str],
                        cert_file: str = "", cert_type: str = "DVSSL"):
    """Request certificate from StartSSL.

        :param csr: The csr with which to request the certificate.
        :param token_id: The token id to present to StartSSL.
        :param client_cert: The client certificate to present to StartSSL.
        :param domains: The list of domains to add to the request.
        :param cert_file: The file name relative to SSL_DIR to store the certificate if issued. Pass "" to disable.
        :param cert_type: The type of certificate to request from StartSSL.
        :return: Returns a tuple containing the StartSSL request status and the certificate in PEM format if successful.
        """

    #
    #
    # ------ INNER CLASS DECLARATIONS ------ #
    #
    #

    # Class to manage making sensitive temporary files securely (ensuring deletion, etc.)
    class make_temp_file:
        def __init__(self, suffix: str = None, prefix: str = None, dir_path: str = None, text: bool = False):
            self.temp_file = tempfile.mkstemp(suffix, prefix, dir_path, text)

        def __enter__(self):
            """Returns a tuple of the file object and its full path."""
            return self.temp_file

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                os.close(self.temp_file[0])
            except OSError:
                # needs to be ignored so that the file as attempted to be destroyed
                print("Temporary file could not be closed.", file=sys.stderr)

            os.remove(self.temp_file[1])  # This file must be destroyed. If it errors out, let the error bubble up

    #
    #
    # ------ START OF FUNCTION ------ #
    #
    #

    # Create temporary file for requests library
    with make_temp_file('tmp', 'ProcessCerts', text=True) as temp_file:

        # Write unencrypted key temporarily to disk, will be destroyed by "with" when done
        os.write(temp_file[0], crypto.dump_certificate(crypto.FILETYPE_PEM, client_cert.get_certificate()) +
                 crypto.dump_privatekey(crypto.FILETYPE_PEM, client_cert.get_privatekey()))

        # Request certificate
        r = requests.post(
            CertSecurityGlobals.DEBUG_API if CertSecurityGlobals.DEBUG_MODE else CertSecurityGlobals.PRODUCTION_API,
            data={'RequestData': json.dumps({
                "tokenID": token_id,
                "actionType": "ApplyCertificate",
                "certType": cert_type,
                "domains": ",".join(domains),
                "CSR": crypto.dump_certificate_request(crypto.FILETYPE_PEM, csr).decode("utf-8")
            })},
            cert=temp_file[1]
        )

        # Decode response
        try:
            response_json = r.json()
        except ValueError:
            raise CertSecurityError(
                "Response sent was not JSON. Type: {0} Response:\n{1}".format(r.headers.get('content-type'), r.text))

        # Bad request
        if not response_json['status'] == 1:
            raise CertSecurityError(response_json['shortMsg'], response_json['errorCode'])

        # CSR issuance status
        if response_json['data']['orderStatus'] == 1:  # The request is pending
            print("The certificate has been successfully submitted and is pending issuance.\n"
                  "\t Order ID: " + response_json['orderID'])
            return response_json['data']['orderStatus'], None

        elif response_json['data']['orderStatus'] == 3:  # The request was rejected
            print("The certificate request has been rejected.", file=sys.stderr)
            return response_json['data']['orderStatus'], None

        elif response_json['data']['orderStatus'] != 2:  # Unspecified error
            raise CertSecurityError(response_json['data']['orderStatus'], "Received an invalid response!")

        else:  # The request was issued
            print("The certificate has been successfully issued.")

            # Get certificate
            cert_received = crypto.load_certificate(crypto.FILETYPE_PEM, base64.b64decode(response_json['data'][
                'certificate'], validate=True))
            inter_cert = crypto.load_certificate(crypto.FILETYPE_PEM, base64.b64decode(response_json['data'][
                'intermediateCertificate'], validate=True))

            # Save certificate if requested
            if cert_file != "":
                for file_format, f in write_all_formats(cert_file):
                    f.write(crypto.dump_certificate(file_format, cert_received))

            return response_json['data']['orderStatus'], cert_received, inter_cert
