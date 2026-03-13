import sys, os, socket, requests, json, urllib, hashlib, struct, ssl, hmac, secrets, base64, time
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from mcp_utils import *
from environment_setup import ensure_environment_setup

discovery_socket_port = 9346
allowed_timestamp_skew_s = 120
SECURITY_SCHEMA_VERSION = 2

SERVER_INFO: dict = {}
SERVER_CERT_PATH = ''
SERVER_SECRET_PATH = ''
SERVER_REQUEST_SESSION: requests.Session | None = None

def configure_stdio():
    """Ensure redirected output can represent UTF-8 build logs on Windows."""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')


class _PinnedCertAdapter(HTTPAdapter):
    def __init__(self, cafile: str, *args, **kwargs):
        self._cafile = cafile
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = ssl.create_default_context(cafile=self._cafile)
        ctx.check_hostname = False
        pool_kwargs['ssl_context'] = ctx
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _bytes_to_sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_hmac_secret() -> str:
    if not SERVER_SECRET_PATH:
        raise RuntimeError('Client HMAC secret path not initialized')
    with open(SERVER_SECRET_PATH, 'r', encoding='utf-8') as f:
        return f.read().strip()


def _sign_http_request(method: str, path_url: str, timestamp: str, nonce: str, body_bytes: bytes) -> str:
    payload = '\n'.join(
        [
            method.upper(),
            path_url,
            timestamp,
            nonce,
            _bytes_to_sha256_hex(body_bytes),
        ]
    )
    secret = _load_hmac_secret().encode('utf-8')
    return hmac.new(secret, payload.encode('utf-8'), hashlib.sha256).hexdigest()


def _get_request_session() -> requests.Session:
    global SERVER_REQUEST_SESSION
    if SERVER_REQUEST_SESSION is None:
        if not SERVER_CERT_PATH or not os.path.exists(SERVER_CERT_PATH):
            raise RuntimeError('Server certificate is not available locally; pairing is required.')
        s = requests.Session()
        s.mount('https://', _PinnedCertAdapter(SERVER_CERT_PATH))
        SERVER_REQUEST_SESSION = s
    return SERVER_REQUEST_SESSION


def _secure_request(
    method: str,
    url: str,
    *,
    params=None,
    data=None,
    json_data=None,
    files=None,
    stream: bool = False,
    timeout: int = 60,
    headers: dict | None = None,
) -> Response:
    session = _get_request_session()
    req_headers = {} if headers is None else dict(headers)
    req = requests.Request(method=method.upper(), url=url, params=params, data=data, json=json_data, files=files, headers=req_headers)
    prepared = session.prepare_request(req)
    body = prepared.body
    if body is None:
        body_bytes = b''
    elif isinstance(body, bytes):
        body_bytes = body
    else:
        body_bytes = str(body).encode('utf-8')
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    signature = _sign_http_request(prepared.method, prepared.path_url, timestamp, nonce, body_bytes)
    prepared.headers['X-RXS-Timestamp'] = timestamp
    prepared.headers['X-RXS-Nonce'] = nonce
    prepared.headers['X-RXS-Signature'] = signature
    return session.send(prepared, stream=stream, timeout=timeout, allow_redirects=True)


def _build_server_url(server_addr: tuple[str, int], path: str) -> str:
    ip, port = server_addr
    return f'https://{ip}:{port}{path}'


def _get_client_security_paths(runtime_dir: str) -> tuple[str, str]:
    cert_dir = os.path.join(runtime_dir, 'certs')
    cred_dir = os.path.join(runtime_dir, 'credentials')
    return os.path.join(cert_dir, 'server_cert.pem'), os.path.join(cred_dir, 'hmac_secret.txt')


def _cert_fingerprint(cert_text: str) -> str:
    return _bytes_to_sha256_hex(cert_text.encode('utf-8'))


def _ensure_pairing_material(serverinfo_path: str, runtime_dir: str, existing_info: dict | None = None) -> dict:
    info = {} if not isinstance(existing_info, dict) else dict(existing_info)
    cert_path, secret_path = _get_client_security_paths(runtime_dir)
    security = info.get('security', {}) if isinstance(info.get('security', {}), dict) else {}
    has_security_files = os.path.exists(cert_path) and os.path.exists(secret_path)
    has_security_meta = (
        isinstance(security.get('cert_relpath', ''), str)
        and isinstance(security.get('secret_relpath', ''), str)
        and isinstance(security.get('cert_fingerprint_sha256', ''), str)
    )
    if has_security_files and has_security_meta and info.get('schema_version') == SECURITY_SCHEMA_VERSION:
        return info

    discovery_result = discover_server(require_pairing=True)
    if discovery_result is None:
        raise RuntimeError('Pairing required. Enable pairing on server and retry within 60 seconds.')
    server_ip, discovery_info = discovery_result
    cert_b64 = discovery_info.get('certificate', '')
    secret = discovery_info.get('secret_key', '')
    if not cert_b64 or not secret:
        raise RuntimeError('Pairing response missing certificate or secret key.')
    try:
        cert_text = base64.b64decode(cert_b64.encode('ascii')).decode('utf-8', errors='replace').strip()
    except Exception as e:
        raise RuntimeError(f'Failed to decode paired certificate payload: {e}') from e
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    os.makedirs(os.path.dirname(secret_path), exist_ok=True)
    with open(cert_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(cert_text + '\n')
    with open(secret_path, 'w', encoding='utf-8') as f:
        f.write(secret.strip())

    info['schema_version'] = SECURITY_SCHEMA_VERSION
    info['server_ip'] = server_ip
    info['server_port'] = int(discovery_info['server_port'])
    info['server_socket_port'] = int(discovery_info['server_socket_port'])
    info['file_socket_port'] = int(discovery_info['file_socket_port'])
    info['security'] = {
        'cert_relpath': unix_path(os.path.relpath(cert_path, runtime_dir)),
        'secret_relpath': unix_path(os.path.relpath(secret_path, runtime_dir)),
        'cert_fingerprint_sha256': _cert_fingerprint(cert_text),
    }
    with open(serverinfo_path, 'w', encoding='utf-8') as f:
        json.dump(info, f)
    return info


def _initialize_security_context(runtime_dir: str, serverinfo_dict: dict) -> None:
    global SERVER_INFO, SERVER_CERT_PATH, SERVER_SECRET_PATH, SERVER_REQUEST_SESSION
    SERVER_INFO = dict(serverinfo_dict)
    security = SERVER_INFO.get('security', {})
    cert_rel = security.get('cert_relpath', 'certs/server_cert.pem')
    secret_rel = security.get('secret_relpath', 'credentials/hmac_secret.txt')
    SERVER_CERT_PATH = os.path.join(runtime_dir, cert_rel)
    SERVER_SECRET_PATH = os.path.join(runtime_dir, secret_rel)
    if not os.path.exists(SERVER_CERT_PATH) or not os.path.exists(SERVER_SECRET_PATH):
        raise RuntimeError('Missing paired security files. Re-run pairing.')
    expected_fp = str(security.get('cert_fingerprint_sha256', '')).strip()
    if expected_fp:
        with open(SERVER_CERT_PATH, 'r', encoding='utf-8') as f:
            actual_fp = _cert_fingerprint(f.read().strip())
        if actual_fp != expected_fp:
            raise RuntimeError('Local server certificate fingerprint mismatch. Re-run pairing.')
    SERVER_REQUEST_SESSION = None


def _make_client_tls_context() -> ssl.SSLContext:
    if not SERVER_CERT_PATH:
        raise RuntimeError('Missing local server certificate path')
    ctx = ssl.create_default_context(cafile=SERVER_CERT_PATH)
    ctx.check_hostname = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _socket_auth_signature(channel: str, session_id: str, timestamp: str, nonce: str) -> str:
    payload = '\n'.join([channel, session_id, timestamp, nonce])
    return hmac.new(_load_hmac_secret().encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()


def _send_socket_auth(conn: socket.socket, channel: str, session_id: str) -> None:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    _send_frame(
        conn,
        {
            'type': 'AUTH',
            'channel': channel,
            'session_id': session_id,
            'timestamp': timestamp,
            'nonce': nonce,
            'signature': _socket_auth_signature(channel, session_id, timestamp, nonce),
        },
    )
    ack, _ = _recv_frame(conn)
    if ack.get('type') != 'AUTH_ACK' or not ack.get('ok', False):
        raise PermissionError(f'Server rejected socket auth for channel {channel}')


def retrieve_file(server_addr:tuple[str, int], path) -> bool:
    app_name = get_appname()
    url = _build_server_url(server_addr, f'/retrieve_files/{app_name}/{path}')
    dct = {'paths': [path]}
    ran_successfully = True
    try:
        resp:Response = _secure_request('GET', url, params=dct)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve file {path}.  Error message: {e}')
        return False

    under_write_count = 0
    
    if len(resp.content) < 1*MB:
        chunk_size = 16 * KB
        with open(path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                num_bytes_written = f.write(chunk)
                if num_bytes_written != chunk_size:
                    under_write_count += 1

    #a single "under_write" (writing less than chunk_size bytes) is normal, and expected to happen... well actually about 99.994% of the time.  So it's pretty normal
    #but I use <=1 instead of just <1 because it IS 99.994% of the time, not 100% (the other 0.006% is when it is 0.  Otherwise something ACTUALLY went wrong)
    ran_successfully = ran_successfully and (under_write_count <= 1)
    return ran_successfully


def discover_server(require_pairing: bool = False) -> tuple[str, dict] | None:
    '''Attempts to discover the server on the local network using UDP broadcasting.  If successful, returns [server_ip, ports_dict], where
        server_ip: Self explanitory, the IP address of the server on the local network
        ports_dict: a dictionary containing relevant port numbers
    '''
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(2.0)

    s.sendto(b'RXS_DISCOVERY_REQ', ('<broadcast>', discovery_socket_port))
    try:
        resp_bytes, addr = s.recvfrom(1*KB)
        resp = resp_bytes.decode()
        discovered_server_ip = addr[0]

    except socket.timeout:
        print('Socket timed out')
        return None

    resp_parts = [line.strip() for line in resp.splitlines() if line.strip()]
    if len(resp_parts) < 2:
        print(f'Too few lines in discovery response from server.  Server returned:\n{resp}')
        return None
    respcode, ports_part = resp_parts[0], resp_parts[1]

    if respcode.strip() != 'RXS_SERVER_HERE':
        print(f'ERROR: Did not receive valid server discovery response code: RXS_SERVER_HERE\nInstead, receieved: {respcode.strip()}')
        return None
    elif not ports_part.startswith('ports|'):
        print(f'ERROR: Did not receive valid port list from server in discovery response.  Receieved: {ports_part}')
        return None
    
    ports_dict_str = ports_part.split('|', 1)[-1]
    response_obj: dict[str, str | int] = {}

    for entry in ports_dict_str.split(','):
        if ':' not in entry:
            print(f'Invalid discovery ports entry: {entry}')
            return None
        key, val = entry.split(':', 1)
        response_obj[key.strip()] = int(val.strip())

    extra_lines = resp_parts[2:]
    allowed_keys = {'certificate', 'secret_key', 'pairing_expires_unix'}
    for line in extra_lines:
        if ':' not in line:
            print(f'Invalid discovery line format: {line}')
            return None
        key, val = line.split(':', 1)
        key = key.strip()
        if key not in allowed_keys:
            print(f'Unknown discovery key: {key}')
            return None
        response_obj[key] = val.strip()

    if require_pairing:
        required_pairing_keys = {'certificate', 'secret_key', 'pairing_expires_unix'}
        if not required_pairing_keys.issubset(set(response_obj.keys())):
            print('Discovery response did not include pairing credentials. Pairing may be disabled or expired.')
            return None
        try:
            pairing_exp = int(str(response_obj['pairing_expires_unix']))
        except ValueError:
            print('Invalid pairing_expires_unix value from discovery response')
            return None
        now = int(time.time())
        if pairing_exp < now - allowed_timestamp_skew_s:
            print('Received expired pairing window in discovery response')
            return None

    return discovered_server_ip, response_obj


def start_build_job(server_addr:tuple[str, int], git_diff_path:str, changed_binary_paths:list[str]=[], args:list[str]=[]) -> str:
    app_name = get_appname()
    url = _build_server_url(server_addr, f'/start-build-job/{app_name}')
    filename = git_diff_path.split('/')[-1]

    files = {'gitdiff': (filename, open(git_diff_path, 'rb'), 'text/plain', {'Expires': 0})}
    for i, path in enumerate(changed_binary_paths):
        path = unix_path(path)
        print(f'Adding {path} to POST request')
        mimetype, encoding = guess_type(path, strict=False)
        if not mimetype:
            mimetype = 'application/octet-stream'
        files[f'binaryfile{i}'] = (path, open(path, 'rb'), mimetype, {'Expires': 0})
    print(f'Starting build job by making POST request to {url} sending a diff file located at {git_diff_path}\n')
    args_data = [('xcodebuild_args', xcodebuild_arg) for xcodebuild_arg in args]
    resp = _secure_request('POST', url, data=args_data, files=files, timeout=300)
    return resp

def check_build_job(server_addr:tuple[str, int], job_id:str, offset:int=0) -> Response:
    url = _build_server_url(server_addr, f'/checkprogress/{job_id}/{offset}')
    resp = _secure_request('GET', url)
    return resp

def wait_for_build_completion(server_addr:tuple[str, int], job_id:str, server_socket_port:int, offset=0) -> str:
    ip, _ = server_addr
    chunk_size = 4096
    full_text = ''
    tls_ctx = _make_client_tls_context()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw_socket:
        raw_socket.connect((ip, server_socket_port))
        with tls_ctx.wrap_socket(raw_socket, server_hostname=ip) as s:
            _send_socket_auth(s, channel='build_log', session_id=str(job_id))
            while True:
                new_bytes = s.recv(chunk_size)
                if not new_bytes:
                    break
                new_text = new_bytes.decode('utf-8', errors='replace')
                print(new_text, end='')
                full_text += new_text

    return full_text


def _recv_exact(s: socket.socket, num_bytes: int) -> bytes:
    data = bytearray()
    while len(data) < num_bytes:
        try:
            chunk = s.recv(num_bytes - len(data))
        except socket.timeout as e:
            raise TimeoutError(
                f'Socket read timed out while waiting for {num_bytes} bytes; received {len(data)} so far'
            ) from e
        if not chunk:
            raise ConnectionError('Socket closed while reading expected bytes')
        data.extend(chunk)
    return bytes(data)


def _send_frame(s: socket.socket, header: dict, payload: bytes = b'') -> None:
    header_bytes = json.dumps(header).encode('utf-8')
    s.sendall(struct.pack('!I', len(header_bytes)))
    s.sendall(header_bytes)
    s.sendall(struct.pack('!I', len(payload)))
    if payload:
        s.sendall(payload)


def _recv_frame(s: socket.socket) -> tuple[dict, bytes]:
    max_header_len = 64 * KB
    max_payload_len = 8 * MB
    header_len = struct.unpack('!I', _recv_exact(s, 4))[0]
    if header_len <= 0 or header_len > max_header_len:
        raise ValueError(f'Invalid frame header length: {header_len}')
    header = json.loads(_recv_exact(s, header_len).decode('utf-8'))
    payload_len = struct.unpack('!I', _recv_exact(s, 4))[0]
    if payload_len < 0 or payload_len > max_payload_len:
        raise ValueError(f'Invalid frame payload length: {payload_len}')
    payload = _recv_exact(s, payload_len) if payload_len else b''
    return header, payload


def _file_sha256(path: str, chunk_size: int = 256 * KB) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _to_repo_relative_posix(path: str, project_root: str) -> str:
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(project_root)
    if not is_subdir(abs_path, abs_root) and abs_path != abs_root:
        raise ValueError(f'Path is outside project root: {path}')
    rel = os.path.relpath(abs_path, abs_root)
    return unix_path(rel)


def _get_safe_local_project_path(rel_path: str, project_root: str) -> str:
    rel = unix_path(str(rel_path)).strip()
    if not rel:
        raise ValueError('Path is empty')
    if '\x00' in rel:
        raise ValueError('Path contains NUL byte')
    if rel.startswith('/'):
        raise ValueError(f'Absolute paths are not allowed: {rel}')
    first_part = rel.split('/')[0]
    if len(first_part) >= 2 and first_part[1] == ':':
        raise ValueError(f'Drive-letter paths are not allowed: {rel}')
    if rel.startswith('//'):
        raise ValueError(f'UNC-like paths are not allowed: {rel}')
    normalized_rel = unix_path(os.path.normpath(rel))
    normalized_parts = [p for p in normalized_rel.split('/') if p not in ['', '.']]
    if any(p == '..' for p in normalized_parts):
        raise ValueError(f'Path traversal is not allowed: {rel}')
    root_abs = os.path.abspath(project_root)
    dest_abs = os.path.abspath(os.path.join(root_abs, normalized_rel))
    if os.path.commonpath([root_abs, dest_abs]) != root_abs:
        raise ValueError(f'Path escapes project root: {rel}')
    return dest_abs


def _send_file_over_socket(
    s: socket.socket,
    abs_path: str,
    rel_path: str,
    expected_size: int,
    expected_sha256: str,
    transfer_id: str,
    chunk_size: int = 64 * KB,
) -> bool:
    _send_frame(
        s,
        {
            'type': 'FILE_START',
            'transfer_id': transfer_id,
            'rel_path': rel_path,
            'size': expected_size,
            'sha256': expected_sha256,
        },
    )
    ack_header, _ = _recv_frame(s)
    if ack_header.get('type') != 'ACK_FILE_START' or not ack_header.get('ok', False):
        print(f'FILE_START rejected for {rel_path}: {ack_header}')
        return False

    total_bytes_sent = 0
    with open(abs_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            _send_frame(s, {'type': 'FILE_CHUNK', 'transfer_id': transfer_id, 'rel_path': rel_path}, chunk)
            total_bytes_sent += len(chunk)

    _send_frame(s, {'type': 'FILE_END', 'transfer_id': transfer_id, 'rel_path': rel_path})
    file_result_header, _ = _recv_frame(s)
    if file_result_header.get('type') != 'FILE_RESULT' or not file_result_header.get('ok', False):
        print(f'FILE_RESULT failed for {rel_path}: {file_result_header}')
        return False
    if total_bytes_sent != expected_size:
        print(f'Unexpected bytes sent for {rel_path}. Expected {expected_size}, sent {total_bytes_sent}')
        return False
    return True


def receive_files_from_server(server_addr: tuple[str, int], paths: list[str] | str, chunk_size: int = 64 * KB) -> bool:
    ip, _ = server_addr
    app_name = get_appname()
    if isinstance(paths, str):
        paths = [paths]
    rel_paths = [unix_path(path) for path in paths]
    transfer_id = str(uuid4())
    init_url = _build_server_url(server_addr, f'/sendfilesfromserver/init/{app_name}')
    init_payload = {'transfer_id': transfer_id, 'paths': rel_paths, 'chunk_size': chunk_size}
    try:
        init_resp = _secure_request('POST', init_url, json_data=init_payload, timeout=120)
        init_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to initialize server->client transfer: {e}')
        return False
    init_obj = init_resp.json()
    if not init_obj.get('ok', False):
        print(f"Server rejected server->client transfer init: {init_obj.get('errors', [])}")
        return False
    expected_files = init_obj.get('files', [])
    expected_map = {entry['rel_path']: entry for entry in expected_files if isinstance(entry, dict)}
    expected_paths = set(expected_map.keys())
    if not expected_paths:
        print('Server->client transfer returned empty manifest')
        return False

    project_root = get_project_root_path(os.getcwd())
    received_verified: set[str] = set()
    current_file = None
    sock_port = int(init_obj['file_socket_port'])
    tls_ctx = _make_client_tls_context()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw_socket:
        raw_socket.connect((ip, sock_port))
        with tls_ctx.wrap_socket(raw_socket, server_hostname=ip) as s:
            s.settimeout(60)
            _send_socket_auth(s, channel='file_download', session_id=transfer_id)
            try:
                while True:
                    header, payload = _recv_frame(s)
                    msg_type = header.get('type', '')
                    incoming_transfer_id = header.get('transfer_id', '')
                    if incoming_transfer_id != transfer_id:
                        _send_frame(s, {'type': 'ERROR', 'ok': False, 'error': 'transfer_id mismatch'})
                        return False

                    if msg_type == 'FILE_START':
                        rel_path = header.get('rel_path', '')
                        if rel_path not in expected_map:
                            _send_frame(s, {'type': 'ACK_FILE_START', 'ok': False, 'rel_path': rel_path})
                            continue
                        try:
                            destination_path = _get_safe_local_project_path(rel_path, project_root)
                        except ValueError as e:
                            print(f'Invalid destination path for {rel_path}: {e}')
                            _send_frame(s, {'type': 'ACK_FILE_START', 'ok': False, 'rel_path': rel_path})
                            continue

                        parent = os.path.dirname(destination_path)
                        if parent and not os.path.exists(parent):
                            os.makedirs(parent, exist_ok=True)
                        temp_path = destination_path + '.part'
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        current_file = {
                            'rel_path': rel_path,
                            'destination_path': destination_path,
                            'temp_path': temp_path,
                            'expected_size': int(expected_map[rel_path]['size']),
                            'expected_sha256': expected_map[rel_path]['sha256'],
                            'bytes_received': 0,
                            'hash': hashlib.sha256(),
                            'handle': open(temp_path, 'wb'),
                        }
                        _send_frame(s, {'type': 'ACK_FILE_START', 'ok': True, 'rel_path': rel_path})

                    elif msg_type == 'FILE_CHUNK':
                        if not current_file:
                            continue
                        current_file['handle'].write(payload)
                        current_file['hash'].update(payload)
                        current_file['bytes_received'] += len(payload)

                    elif msg_type == 'FILE_END':
                        rel_path = header.get('rel_path', '')
                        if not current_file or rel_path != current_file['rel_path']:
                            _send_frame(s, {'type': 'FILE_RESULT', 'ok': False, 'rel_path': rel_path})
                            continue
                        current_file['handle'].close()
                        actual_size = current_file['bytes_received']
                        actual_sha256 = current_file['hash'].hexdigest()
                        verified = (
                            actual_size == current_file['expected_size']
                            and actual_sha256 == current_file['expected_sha256']
                        )
                        if verified:
                            os.replace(current_file['temp_path'], current_file['destination_path'])
                            received_verified.add(rel_path)
                        else:
                            if os.path.exists(current_file['temp_path']):
                                os.remove(current_file['temp_path'])
                        _send_frame(s, {'type': 'FILE_RESULT', 'ok': verified, 'rel_path': rel_path})
                        current_file = None

                    elif msg_type == 'TRANSFER_END':
                        missing_paths = sorted(expected_paths - received_verified)
                        ok = len(missing_paths) == 0
                        _send_frame(
                            s,
                            {
                                'type': 'TRANSFER_RECEIVED',
                                'ok': ok,
                                'transfer_id': transfer_id,
                                'missing': missing_paths,
                            },
                        )
                        break
                    else:
                        print(f'Unknown frame type from server: {msg_type}')
                        return False
            except TimeoutError as e:
                active_rel_path = current_file.get('rel_path', '') if isinstance(current_file, dict) else ''
                if active_rel_path:
                    print(f'Timed out receiving server file transfer while handling {active_rel_path}: {e}')
                else:
                    print(f'Timed out receiving server file transfer: {e}')
                return False
            except (ConnectionError, ValueError, json.JSONDecodeError) as e:
                print(f'Protocol/connection error while receiving files from server: {e}')
                return False
            finally:
                handle = None
                temp_path = ''
                if isinstance(current_file, dict):
                    handle = current_file.get('handle', None)
                    temp_path = current_file.get('temp_path', '')
                if handle is not None and hasattr(handle, 'closed') and not handle.closed:
                    handle.close()
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

    complete_url = _build_server_url(server_addr, f'/sendfilesfromserver/complete/{app_name}')
    try:
        complete_resp = _secure_request('POST', complete_url, json_data={'transfer_id': transfer_id})
        complete_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to complete server->client transfer: {e}')
        return False
    complete_obj = complete_resp.json()
    if not complete_obj.get('ok', False):
        print(f"Server->client transfer completion failed: {complete_obj.get('errors', [])}")
        return False
    return True


def send_files(server_addr:tuple[str, int], paths:list[str]|str, filesize_threshold:int=20*MB, total_threshold=50*MB) -> bool:
    app_name = get_appname()
    if isinstance(paths, str):
        paths = [paths]
    project_root = get_project_root_path(os.getcwd())
    file_entries = []
    for path in paths:
        abs_path = os.path.abspath(path)
        rel_path = _to_repo_relative_posix(abs_path, project_root)
        size = os.path.getsize(abs_path)
        sha256 = _file_sha256(abs_path)
        file_entries.append({'abs_path': abs_path, 'rel_path': rel_path, 'size': size, 'sha256': sha256})

    file_sizes = [entry['size'] for entry in file_entries]
    if all(size < filesize_threshold for size in file_sizes) and (sum(file_sizes) < total_threshold):
        url = _build_server_url(server_addr, f'/sendfileshttp/{app_name}')
        files = {}
        handles = []
        try:
            for i, entry in enumerate(file_entries):
                rel_path = entry['rel_path']
                abs_path = entry['abs_path']
                path = unix_path(abs_path)

                mimetype, encoding = guess_type(path, strict=False)
                if not mimetype:
                    if is_plaintext(path):
                        mimetype = 'text/plain'
                    else:
                        mimetype = 'application/octet-stream'
                handle = open(abs_path, 'rb')
                handles.append(handle)
                files[f'file{i}'] = (rel_path, handle, mimetype, {'Expires': 0})

            resp = _secure_request('POST', url, files=files, timeout=300)
        finally:
            for handle in handles:
                handle.close()
        resp.raise_for_status()
        result = resp.json()
        print(f'result: {result}')
        if not result.get('ok', False):
            print(f'HTTP file transfer failed: {result}')
            return False
        return True

    transfer_id = str(uuid4())
    url = _build_server_url(server_addr, f'/sendfilessocket/init/{app_name}')
    init_payload = {
        'transfer_id': transfer_id,
        'chunk_size': 64 * KB,
        'files': [
            {'rel_path': entry['rel_path'], 'size': entry['size'], 'sha256': entry['sha256']}
            for entry in file_entries
        ],
    }
    init_resp = None
    last_err = None
    for _ in range(3):
        try:
            init_resp = _secure_request('POST', url, json_data=init_payload, timeout=120)
            init_resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
    if init_resp is None:
        print(f'Failed to initialize socket transfer: {last_err}')
        return False
    init_obj = init_resp.json()
    if not init_obj.get('ok', False):
        print(f"Server rejected init for transfer {transfer_id}: {init_obj.get('errors', [])}")
        return False

    sock_port = int(init_obj['file_socket_port'])
    ip, _ = server_addr
    tls_ctx = _make_client_tls_context()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw_socket:
        raw_socket.connect((ip, sock_port))
        with tls_ctx.wrap_socket(raw_socket, server_hostname=ip) as s:
            s.settimeout(60)
            _send_socket_auth(s, channel='file_upload', session_id=transfer_id)
            for entry in file_entries:
                sent_successfully = _send_file_over_socket(
                    s=s,
                    abs_path=entry['abs_path'],
                    rel_path=entry['rel_path'],
                    expected_size=entry['size'],
                    expected_sha256=entry['sha256'],
                    transfer_id=transfer_id,
                    chunk_size=init_payload['chunk_size'],
                )
                if not sent_successfully:
                    print(f"Failed to send file over socket: {entry['rel_path']}")
                    return False
            _send_frame(s, {'type': 'TRANSFER_END', 'transfer_id': transfer_id})
            transfer_ack, _ = _recv_frame(s)
            if transfer_ack.get('type') != 'TRANSFER_RECEIVED' or not transfer_ack.get('ok', False):
                print(f'Server did not accept transfer end: {transfer_ack}')
                return False

    complete_url = _build_server_url(server_addr, f'/sendfilessocket/complete/{app_name}')
    complete_resp = _secure_request('POST', complete_url, json_data={'transfer_id': transfer_id})
    complete_resp.raise_for_status()
    complete_obj = complete_resp.json()
    if not complete_obj.get('ok', False):
        print(f"Transfer completion failed for {transfer_id}: {complete_obj.get('errors', [])}")
        return False
    return True



def apply_patch_server(server_addr:tuple[str, int], patch_path:str=None) -> Response:
    app_name = get_appname()
    if not patch_path:
        patch_path = os.path.join(get_runtime_dir_path(), 'gitdiff.diff')
    if not os.path.exists(patch_path):
        patch_path, _ = prepare_text_changes()
        git_diff_filepath
    url = _build_server_url(server_addr, f'/apply-patch-server/{app_name}')
    resp:Response = _secure_request('GET', url, params={'patch_path': patch_path})
    return resp
    


#Sort of like git push, but for uncommited changes and specifically from the server
def send_current_changes(server_addr:tuple[str, int]) -> bool:
    git_diff_path, changed_binary_paths = prepare_text_changes()
    paths = [os.path.join(get_runtime_dir_path(), 'gitdiff.diff'), *changed_binary_paths]
    print('sending paths:')
    print(paths)
    send_files(server_addr, paths)
    app_name = get_appname()
    ip, port = server_addr
    # Server route expects a repo-relative patch path under query key "patch_path".
    patch_rel_path = _to_repo_relative_posix(git_diff_path, get_project_root_path())
    resp:Response = apply_patch_server(server_addr, patch_rel_path)
    resp.raise_for_status()
    return True

def retrieve_current_text_changes(server_addr:tuple[str, int], save_as_filename='gitdiff.diff') -> bool:
    app_name = get_appname()
    url = _build_server_url(server_addr, f'/retrieve_text_changes/{app_name}')
    ran_successfully = True
    try:
        diff_resp:Response = _secure_request('GET', url, stream=True, timeout=120)
        diff_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve text changes: {e}')
        return False

    if not diff_resp.content:
        print('Received empty diff file')

    runtime_dir = get_runtime_dir_path()
    git_patch_path = os.path.join(runtime_dir, save_as_filename)
    with open(git_patch_path, 'wb') as f:
        f.write(diff_resp.content)

    apply_patch(git_patch_path)
    return ran_successfully


#Sort of like git pull, but for uncommitted changes and specifically to the server
def retrieve_current_changes(server_addr:tuple[str, int], exclude_binary_changes=False, save_as_filename='gitdiff.diff') -> bool:
    retrieved_text_changes = retrieve_current_text_changes(server_addr, save_as_filename)
    if not exclude_binary_changes:
        project_root = get_project_root_path()
        app_name = get_appname()
        url = _build_server_url(server_addr, f'/retrieve_changed_binary_paths/{app_name}')
        try:
            binary_paths_resp:Response = _secure_request('GET', url)
            binary_paths_resp.raise_for_status()
        except requests.RequestException as e:
            print(f'Failed to retrieve binary path list: {e}')
            return False
        if not binary_paths_resp.text:
            print('No changed binary files returned by server')

        #split binary file paths into a list
        paths = [path.strip() for path in binary_paths_resp.text.split('\n')]
        paths = [_to_repo_relative_posix(path, project_root) for path in paths if path] #remove empty paths
        if paths: #only send binary changes if there are binary changes to be sent.  Otherwise we get a 400 error
            retrieved_binary_changes = receive_files_from_server(server_addr, paths)
            return retrieved_text_changes and retrieved_binary_changes
    return retrieved_text_changes




RECONCILE_STATUS_ALIGNED = 'ALIGNED'
RECONCILE_STATUS_RECONCILED = 'RECONCILED'
RECONCILE_STATUS_BLOCKED_DIRTY_WORKTREE = 'BLOCKED_DIRTY_WORKTREE'
RECONCILE_STATUS_BLOCKED_MISSING_COMMIT_OBJECT = 'BLOCKED_MISSING_COMMIT_OBJECT'
RECONCILE_STATUS_BLOCKED_DIVERGED_HISTORY = 'BLOCKED_DIVERGED_HISTORY'
RECONCILE_STATUS_BLOCKED_DETACHED_HEAD = 'BLOCKED_DETACHED_HEAD'
RECONCILE_STATUS_BLOCKED_NO_ORIGIN = 'BLOCKED_NO_ORIGIN'
RECONCILE_STATUS_ERROR = 'ERROR'
RECONCILE_STATUS_NEEDS_ACTION = 'NEEDS_ACTION'


def _reconcile_result(
    status:str,
    authority_side:str='none',
    target_branch:str='',
    target_commit:str='',
    actions_applied:list[str]|None=None,
    message:str='',
) -> dict:
    '''A helper function that returns the arguments passed in as a dict, sanitizing [actions_applied] to [] if it is None'''
    if actions_applied is None:
        actions_applied = []
    return {
        'status': status,
        'authority_side': authority_side,
        'target_branch': target_branch,
        'target_commit': target_commit,
        'actions_applied': actions_applied,
        'message': message,
    }


def get_local_git_state() -> dict:
    project_root = get_project_root_path()
    return get_git_state(project_root)


def get_server_git_state(server_addr:tuple[str, int], app_name:str='') -> dict|None:
    if not app_name:
        app_name = get_appname()

    url = _build_server_url(server_addr, f'/git_state/{app_name}')
    try:
        resp:Response = _secure_request('GET', url)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve server git state: {e}')
        return None

    try:
        obj = resp.json()
    except ValueError:
        print('Server git state response was not valid JSON')
        return None

    return obj


def _post_server_git_action(server_addr:tuple[str, int], action:str, args:dict|None=None, app_name:str='') -> dict|None:
    if not app_name:
        app_name = get_appname()
    if args is None:
        args = {}

    url = _build_server_url(server_addr, f'/git_action/{app_name}')
    payload = {'action': action, 'args': args}
    try:
        resp:Response = _secure_request('POST', url, json_data=payload)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to run server git action {action}: {e}')
        return None

    try:
        return resp.json()
    except ValueError:
        print(f'Server git action response was not valid JSON for action: {action}')
        return None


def _run_local_git_action(action:str, args:dict|None=None) -> dict:
    return execute_git_action(action, args=args, cwd=get_project_root_path())


def _get_reconcile_bundle_paths(filename:str='update.bundle') -> tuple[str, str]:
    project_root = get_project_root_path()
    runtime_dir = get_runtime_dir_path(project_root)
    bundle_abs_path = os.path.join(runtime_dir, filename)
    bundle_rel_path = _to_repo_relative_posix(bundle_abs_path, project_root)
    return bundle_abs_path, bundle_rel_path


def _format_action_failure(action_label:str, result:dict|None) -> str:
    if result is None:
        return f'Failed action {action_label}: no response returned.'

    stderr_text = str(result.get('stderr', '')).strip()
    stdout_text = str(result.get('stdout', '')).strip()
    error_text = str(result.get('error', '')).strip()
    returncode = result.get('returncode', None)

    detail = stderr_text or stdout_text or error_text
    if detail:
        if returncode is not None:
            return f'Failed action {action_label} (returncode={returncode}): {detail}'
        return f'Failed action {action_label}: {detail}'

    if returncode is not None:
        return f'Failed action {action_label} (returncode={returncode}).'
    return f'Failed action {action_label}.'


def _is_gitignore_overwrite_conflict(result:dict|None) -> bool:
    if result is None:
        return False
    stderr_text = str(result.get('stderr', '')).lower()
    stdout_text = str(result.get('stdout', '')).lower()
    error_text = str(result.get('error', '')).lower()
    combined = f'{stderr_text}\n{stdout_text}\n{error_text}'
    return '.gitignore' in combined and 'would be overwritten by checkout' in combined


def compute_reconcile_decision(local_state:dict, server_state:dict) -> dict:
    local_head = local_state.get('head', '')
    server_head = server_state.get('head', '')
    local_branch = local_state.get('current_branch', '')
    server_branch = server_state.get('current_branch', '')

    if local_head == server_head and local_branch == server_branch:
        return _reconcile_result(
            RECONCILE_STATUS_ALIGNED,
            authority_side='none',
            target_branch=local_branch,
            target_commit=local_head,
            message='Client and server are already on the same branch and commit.',
        )

    if local_head == server_head and local_branch != server_branch:
        if local_state.get('is_detached', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_DETACHED_HEAD,
                authority_side='client',
                target_branch='',
                target_commit=local_head,
                message='Client is detached; cannot use detached HEAD as authoritative branch target.',
            )
        return _reconcile_result(
            RECONCILE_STATUS_NEEDS_ACTION,
            authority_side='client',
            target_branch=local_branch,
            target_commit=local_head,
            message='Commits match but branches differ; client branch is authoritative.',
        )

    ahead_behind = git_ahead_behind(local_head, server_head, cwd=get_project_root_path())
    if ahead_behind is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side='none',
            message='Failed to compute ahead/behind relationship between client and server commits.',
        )

    client_ahead, server_ahead = ahead_behind
    if client_ahead > 0 and server_ahead > 0:
        return _reconcile_result(
            RECONCILE_STATUS_BLOCKED_DIVERGED_HISTORY,
            authority_side='none',
            message='Client and server histories have diverged and require manual merge/rebase.',
        )

    if client_ahead > 0 and server_ahead == 0:
        if local_state.get('is_detached', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_DETACHED_HEAD,
                authority_side='client',
                target_branch='',
                target_commit=local_head,
                message='Client is ahead but detached; cannot reconcile to detached authoritative HEAD.',
            )
        return _reconcile_result(
            RECONCILE_STATUS_NEEDS_ACTION,
            authority_side='client',
            target_branch=local_branch,
            target_commit=local_head,
            message='Client is strictly ahead and is the authoritative target.',
        )

    if server_ahead > 0 and client_ahead == 0:
        if server_state.get('is_detached', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_DETACHED_HEAD,
                authority_side='server',
                target_branch='',
                target_commit=server_head,
                message='Server is ahead but detached; cannot reconcile to detached authoritative HEAD.',
            )
        return _reconcile_result(
            RECONCILE_STATUS_NEEDS_ACTION,
            authority_side='server',
            target_branch=server_branch,
            target_commit=server_head,
            message='Server is strictly ahead and is the authoritative target.',
        )

    # If ahead/behind reports equal commits, default to client branch authority.
    if local_state.get('is_detached', False):
        return _reconcile_result(
            RECONCILE_STATUS_BLOCKED_DETACHED_HEAD,
            authority_side='client',
            target_branch='',
            target_commit=local_head,
            message='Client is detached; cannot use detached HEAD as authoritative branch target.',
        )
    return _reconcile_result(
        RECONCILE_STATUS_NEEDS_ACTION,
        authority_side='client',
        target_branch=local_branch,
        target_commit=local_head,
        message='Commits are equivalent; client branch is authoritative.',
    )


def apply_reconcile_actions(server_addr:tuple[str, int], decision:dict) -> dict:
    if decision.get('status') != RECONCILE_STATUS_NEEDS_ACTION:
        return decision

    app_name = get_appname()
    authority_side = decision.get('authority_side', 'none')
    target_branch = decision.get('target_branch', '')
    target_commit = decision.get('target_commit', '')
    actions_applied = list(decision.get('actions_applied', []))

    if authority_side not in ['client', 'server']:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side='none',
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=actions_applied,
            message=f'Invalid authority side in decision: {authority_side}',
        )

    non_authoritative_side = 'server' if authority_side == 'client' else 'client'

    def read_side_state(side:str) -> dict|None:
        if side == 'client':
            try:
                return get_local_git_state()
            except Exception as e:
                print(f'Failed to read local git state: {e}')
                return None
        return get_server_git_state(server_addr, app_name)

    def run_side_action(side:str, action:str, args:dict) -> dict|None:
        if side == 'client':
            result = _run_local_git_action(action, args)
        else:
            result = _post_server_git_action(server_addr, action, args=args, app_name=app_name)
        actions_applied.append(f'{side}:{action}')
        return result

    target_side_state = read_side_state(non_authoritative_side)
    if target_side_state is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=actions_applied,
            message=f'Failed to read {non_authoritative_side} git state before applying reconcile actions.',
        )

    if target_branch in target_side_state.get('branches', []):
        first_action = 'checkout_branch'
        first_args = {'branch': target_branch}
    else:
        first_action = 'checkout_branch_at_commit'
        first_args = {'branch': target_branch, 'commit': target_commit}

    first_result = run_side_action(non_authoritative_side, first_action, first_args)
    if not first_result or not first_result.get('success', False):
        if first_action == 'checkout_branch_at_commit' and _is_gitignore_overwrite_conflict(first_result):
            backup_result = run_side_action(non_authoritative_side, 'backup_remove_gitignore', {})
            if backup_result and backup_result.get('success', False):
                retry_result = run_side_action(non_authoritative_side, first_action, first_args)
                if retry_result and retry_result.get('success', False):
                    target_side_state = read_side_state(non_authoritative_side)
                    if target_side_state is None:
                        return _reconcile_result(
                            RECONCILE_STATUS_ERROR,
                            authority_side=authority_side,
                            target_branch=target_branch,
                            target_commit=target_commit,
                            actions_applied=actions_applied,
                            message=f'Failed to read {non_authoritative_side} git state after retry checkout step.',
                        )
                else:
                    action_label = f'{non_authoritative_side}:{first_action}'
                    return _reconcile_result(
                        RECONCILE_STATUS_ERROR,
                        authority_side=authority_side,
                        target_branch=target_branch,
                        target_commit=target_commit,
                        actions_applied=actions_applied,
                        message=_format_action_failure(action_label, retry_result),
                    )
            else:
                backup_label = f'{non_authoritative_side}:backup_remove_gitignore'
                return _reconcile_result(
                    RECONCILE_STATUS_ERROR,
                    authority_side=authority_side,
                    target_branch=target_branch,
                    target_commit=target_commit,
                    actions_applied=actions_applied,
                    message=_format_action_failure(backup_label, backup_result),
                )
        else:
            action_label = f'{non_authoritative_side}:{first_action}'
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                authority_side=authority_side,
                target_branch=target_branch,
                target_commit=target_commit,
                actions_applied=actions_applied,
                message=_format_action_failure(action_label, first_result),
            )

    target_side_state = read_side_state(non_authoritative_side)
    if target_side_state is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=actions_applied,
            message=f'Failed to read {non_authoritative_side} git state after checkout step.',
        )

    if target_side_state.get('head', '') != target_commit:
        ff_result = run_side_action(non_authoritative_side, 'ff_only_to_commit', {'commit': target_commit})
        if not ff_result or not ff_result.get('success', False):
            action_label = f'{non_authoritative_side}:ff_only_to_commit'
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                authority_side=authority_side,
                target_branch=target_branch,
                target_commit=target_commit,
                actions_applied=actions_applied,
                message=_format_action_failure(action_label, ff_result),
            )

    return _reconcile_result(
        RECONCILE_STATUS_NEEDS_ACTION,
        authority_side=authority_side,
        target_branch=target_branch,
        target_commit=target_commit,
        actions_applied=actions_applied,
        message=decision.get('message', ''),
    )


def reconcile_git_state(server_addr:tuple[str, int], app_name:str='') -> dict:
    if not app_name:
        app_name = get_appname()
    actions_applied:list[str] = []

    try:
        local_state = get_local_git_state()
    except Exception as e:
        return _reconcile_result(RECONCILE_STATUS_ERROR, message=f'Failed to read local git state: {e}')

    server_state = get_server_git_state(server_addr, app_name)
    if server_state is None:
        return _reconcile_result(RECONCILE_STATUS_ERROR, message='Failed to retrieve server git state.')

    local_head = local_state.get('head', '')
    server_head = server_state.get('head', '')
    local_branch = local_state.get('current_branch', '')
    server_branch = server_state.get('current_branch', '')

    if local_head == server_head and local_branch == server_branch:
        return _reconcile_result(
            RECONCILE_STATUS_ALIGNED,
            authority_side='none',
            target_branch=local_branch,
            target_commit=local_head,
            actions_applied=actions_applied,
            message='Client and server are already aligned.',
        )

    needs_git_change = local_head != server_head or local_branch != server_branch
    if needs_git_change and (local_state.get('dirty_tracked', False) or server_state.get('dirty_tracked', False)):
        return _reconcile_result(
            RECONCILE_STATUS_BLOCKED_DIRTY_WORKTREE,
            authority_side='none',
            target_branch='',
            target_commit='',
            actions_applied=actions_applied,
            message='Tracked uncommitted changes detected; reconcile requires a clean tracked worktree on both sides. No fetch or bundle transfer was attempted.',
        )

    def recheck_commit_visibility() -> tuple[bool, bool] | None:
        local_visibility = git_has_commit(server_head, cwd=get_project_root_path())
        server_has_local_result_inner = _post_server_git_action(
            server_addr,
            'has_commit',
            args={'commit': local_head},
            app_name=app_name,
        )
        if server_has_local_result_inner is None:
            return None
        server_visibility = bool(server_has_local_result_inner.get('has_commit', False))
        return local_visibility, server_visibility

    initial_visibility = recheck_commit_visibility()
    if initial_visibility is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            actions_applied=actions_applied,
            message='Failed while checking whether server can resolve client HEAD commit.',
        )
    local_has_server_head, server_has_local_head = initial_visibility

    fetch_failures:list[str] = []
    if not local_has_server_head:
        fetch_local = _run_local_git_action('fetch_origin', {})
        actions_applied.append('client:fetch_origin')
        if not fetch_local.get('success', False):
            fetch_failures.append(_format_action_failure('client:fetch_origin', fetch_local))

    if not server_has_local_head:
        fetch_server = _post_server_git_action(server_addr, 'fetch_origin', args={}, app_name=app_name)
        actions_applied.append('server:fetch_origin')
        if not fetch_server or not fetch_server.get('success', False):
            fetch_failures.append(_format_action_failure('server:fetch_origin', fetch_server))

    # Re-check commit visibility after fetch attempts.
    post_fetch_visibility = recheck_commit_visibility()
    if post_fetch_visibility is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            actions_applied=actions_applied,
            message='Failed while re-checking server commit visibility after fetch.',
        )
    local_has_server_head, server_has_local_head = post_fetch_visibility

    bundle_fallback_attempted = False
    if not local_has_server_head and server_has_local_head:
        bundle_abs_path, bundle_rel_path = _get_reconcile_bundle_paths()
        create_server_bundle = _post_server_git_action(
            server_addr,
            'create_update_bundle',
            {'path': bundle_rel_path, 'start_ref': local_head, 'end_ref': 'HEAD'},
            app_name=app_name,
        )
        actions_applied.append('server:create_update_bundle')
        if not create_server_bundle or not create_server_bundle.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message=_format_action_failure('server:create_update_bundle', create_server_bundle),
            )

        success = receive_files_from_server(server_addr, [bundle_rel_path])
        actions_applied.append('client:receive_update_bundle')
        if not success:
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message='Failed to transfer update bundle from server.',
            )

        if not os.path.exists(bundle_abs_path):
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message=f'Update bundle missing after server transfer: {bundle_abs_path}',
            )

        apply_local_bundle = _run_local_git_action('apply_update_bundle', {'path': bundle_abs_path})
        actions_applied.append('client:apply_update_bundle')
        if not apply_local_bundle.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message=_format_action_failure('client:apply_update_bundle', apply_local_bundle),
            )
        bundle_fallback_attempted = True
    elif local_has_server_head and not server_has_local_head:
        bundle_abs_path, bundle_rel_path = _get_reconcile_bundle_paths()
        create_local_bundle = _run_local_git_action(
            'create_update_bundle',
            {'path': bundle_abs_path, 'start_ref': server_head, 'end_ref': 'HEAD'},
        )
        actions_applied.append('client:create_update_bundle')
        if not create_local_bundle.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message=_format_action_failure('client:create_update_bundle', create_local_bundle),
            )

        sent_bundle = send_files(server_addr, [bundle_abs_path])
        actions_applied.append('client:send_update_bundle')
        if not sent_bundle:
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message='Failed to send update bundle to server.',
            )

        apply_server_bundle = _post_server_git_action(
            server_addr,
            'apply_update_bundle',
            args={'path': bundle_rel_path},
            app_name=app_name,
        )
        actions_applied.append('server:apply_update_bundle')
        if not apply_server_bundle or not apply_server_bundle.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message=_format_action_failure('server:apply_update_bundle', apply_server_bundle),
            )
        bundle_fallback_attempted = True
    elif not local_has_server_head and not server_has_local_head:
        message = 'both_sides_missing_commits_after_fetch; bundle_fallback_skipped'
        if fetch_failures:
            message = f'{message}; {' | '.join(fetch_failures)}'
        return _reconcile_result(
            RECONCILE_STATUS_BLOCKED_MISSING_COMMIT_OBJECT,
            actions_applied=actions_applied,
            message=message,
        )

    if bundle_fallback_attempted:
        post_bundle_visibility = recheck_commit_visibility()
        if post_bundle_visibility is None:
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                actions_applied=actions_applied,
                message='Failed while re-checking server commit visibility after bundle fallback.',
            )
        local_has_server_head, server_has_local_head = post_bundle_visibility

    if not local_has_server_head or not server_has_local_head:
        missing_bits = []
        if not local_has_server_head:
            missing_bits.append('client_missing_server_commit')
        if not server_has_local_head:
            missing_bits.append('server_missing_client_commit')
        if fetch_failures:
            missing_bits.extend(fetch_failures)
        return _reconcile_result(
            RECONCILE_STATUS_BLOCKED_MISSING_COMMIT_OBJECT,
            actions_applied=actions_applied,
            message='; '.join(missing_bits),
        )

    decision = compute_reconcile_decision(local_state, server_state)
    decision['actions_applied'] = actions_applied + list(decision.get('actions_applied', []))

    if decision['status'] in [
        RECONCILE_STATUS_ALIGNED,
        RECONCILE_STATUS_BLOCKED_DIVERGED_HISTORY,
        RECONCILE_STATUS_BLOCKED_DETACHED_HEAD,
        RECONCILE_STATUS_ERROR,
    ]:
        return decision

    if decision['status'] != RECONCILE_STATUS_NEEDS_ACTION:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            actions_applied=decision.get('actions_applied', []),
            message=f'Unexpected decision status: {decision["status"]}',
        )

    applied = apply_reconcile_actions(server_addr, decision)
    if applied['status'] == RECONCILE_STATUS_ERROR:
        return applied

    target_commit = applied.get('target_commit', '')
    target_branch = applied.get('target_branch', '')
    authority_side = applied.get('authority_side', 'none')
    all_actions = applied.get('actions_applied', [])

    try:
        final_local = get_local_git_state()
    except Exception as e:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=all_actions,
            message=f'Failed to read final local git state: {e}',
        )
    final_server = get_server_git_state(server_addr, app_name)
    if final_server is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=all_actions,
            message='Failed to read final server git state.',
        )

    if (
        final_local.get('head', '') == target_commit
        and final_server.get('head', '') == target_commit
        and final_local.get('current_branch', '') == target_branch
        and final_server.get('current_branch', '') == target_branch
    ):
        return _reconcile_result(
            RECONCILE_STATUS_RECONCILED,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=all_actions,
            message='Git state reconciled successfully.',
        )

    return _reconcile_result(
        RECONCILE_STATUS_ERROR,
        authority_side=authority_side,
        target_branch=target_branch,
        target_commit=target_commit,
        actions_applied=all_actions,
        message='Post-reconcile validation failed; client/server branch and commit do not match target.',
    )


def retrieve_diff_for_files(server_addr:tuple[str, int], paths:list[str]) -> str:
    app_name:str = get_appname()
    url = _build_server_url(server_addr, f'/retrieve_diff_for_files/{app_name}')
    try:
        resp:Response = _secure_request('POST', url, json_data={'filepaths': paths}, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve diff for specified files: {','.join(paths)[1:]}')
        return []

    runtime_dir_path = get_runtime_dir_path()
    git_diff_name = 'specific_files_gitdiff.diff'
    git_diff_path = os.path.join(runtime_dir_path, git_diff_name)

    with open(git_diff_path, 'wb') as diff_file:
        KB = 1024
        diff_size = len(resp.content)
        #if diff is over 0.5MB
        if diff_size > 512 * KB:
            chunk_size = 32 * KB
            i = 0
            while i < diff_size:
                chunk = resp.content[i:i+chunk_size]
                diff_file.write(chunk)
                i += chunk_size
        else: #diff_size is less than 0.5MB, just write it all in one go
            diff_file.write(resp.content)

    return git_diff_path




    




def sync_changes_with_server(server_addr:tuple[str, int], sync_branches=False, scope='repo') -> bool:
    app_name = get_appname()

    ########################### Reconcile actual git state (commit/branch) between client and server ###########################
    reconcile_result = reconcile_git_state(server_addr, app_name=app_name)
    reconcile_status = reconcile_result.get('status', RECONCILE_STATUS_ERROR)
    if reconcile_status not in [RECONCILE_STATUS_ALIGNED, RECONCILE_STATUS_RECONCILED]:
        print(f'Phase 1 reconcile blocked: {reconcile_status}')
        print(f"Reason: {reconcile_result.get('message', '')}")
        if reconcile_result.get('actions_applied'):
            print(f"Actions applied before stop: {', '.join(reconcile_result['actions_applied'])}")
        return False
    ############################################################################################################################  

    ################################################# Sync uncommitted changes #################################################
    url = _build_server_url(server_addr, f'/retrieve_changed_file_paths/{app_name}/{scope}')
    changed_fpaths_client = get_changed_file_paths(scope)
    changed_plainpaths_client, changed_binarypaths_client = split_paths_by_text_or_binary(changed_fpaths_client)

    try:
        resp = _secure_request('GET', url, stream=True, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve changed file path lst: {e}')
        return False
    if not resp.text:
        print(f'No changed files returned by server (reason not specified)')
        return False

    resp_json_obj:dict = json.loads(resp.text)
    changed_plainpaths_server = resp_json_obj['plaintext_file_paths']
    changed_binarypaths_server = resp_json_obj['binary_file_paths']

    print(f'resp_json_obj: {resp_json_obj}')

    #Lists of paths of files that only have changes on the server
    server_only_plaintext_paths = [path for path in changed_plainpaths_server if path not in changed_plainpaths_client]
    server_only_binary_paths = [path for path in changed_binarypaths_server if path not in changed_binarypaths_client]

    #Lists of paths of files that only have changes on the client
    client_only_plaintext_paths = [path for path in changed_plainpaths_client if path not in changed_plainpaths_server]
    client_only_binary_paths = [path for path in changed_binarypaths_client if path not in changed_binarypaths_server]

    #Lists of paths of files that have changes on both the client and ther server.  Note this does NOT necessarily mean that they have the SAME changes
    shared_plaintext_paths = [path for path in changed_plainpaths_client if path in changed_plainpaths_server]
    shared_binary_paths = [path for path in changed_binarypaths_client if path in changed_binarypaths_server]

    #if there are any plaintext files only on the server, retrieve the diff for those specific files, and then apply the patch locally on the client
    if server_only_plaintext_paths:
        diff_path = retrieve_diff_for_files(server_addr, server_only_plaintext_paths)
        apply_patch(diff_path)

    #if there are any binary files only on the server, retrieve each files and save to the same path locally on the client
    if server_only_binary_paths:
        success = receive_files_from_server(server_addr, server_only_binary_paths)
        if not success:
            print('Failed to retrieve binary files from server via socket transfer')
            return False
    #if there are any plaintext files only on the client, retrieve the diff for those specific files, and then apply the patch locally on the server
    if client_only_plaintext_paths:
        project_root_path = get_project_root_path()
        client_diff_path = get_diff_for_files(client_only_plaintext_paths, 'client_plaintext_diff.diff')
        client_diff_path = _to_repo_relative_posix(client_diff_path, project_root_path)
        success = send_files(server_addr, [client_diff_path])
        if not success:
            print(f'Failed to send client plaintext files patch to server')
            return False
        resp = apply_patch_server(server_addr, client_diff_path)
        resp.raise_for_status()
        
    if client_only_binary_paths:
        success = send_files(server_addr, client_only_binary_paths)
        if not success:
            print(f'Failed to send new/changed client binary files to the server')

    #####      Handle this later     #####
    if shared_plaintext_paths:
        #we can check if it is possible to apply both sets of changes without conflicts.  This may/can be possible
        pass
    if shared_binary_paths:
        print('There are shared binary files with changes.  Please resolve this manually')









    # print(f'changed_filepaths')
    # for path in changed_filepaths:
    #     print(path)




    


    





    return True
    



#for now, temporarily, I will just impose the requirement that the client must be run from the project root.  This will let me get a testable version much more quickly.  I can improve it from there


if __name__ == '__main__':
    configure_stdio()
    if not os.path.exists(os.path.join(os.path.expanduser('~'), '.remote_xcode_env')):
        ensure_environment_setup()

    cwd = unix_path(os.getcwd())
    BUILD_SUCCESS = '** BUILD SUCCEEDED **'
    BUILD_FAILED = '** BUILD FAILED **'



    runtime_dir_name = get_runtime_dir_name()
    runtime_dir = unix_path(os.path.join(cwd, runtime_dir_name))
    gitignore_path = os.path.join(cwd, '.gitignore')
    git_diff_filepath = unix_path(os.path.join(runtime_dir, 'gitdiff.diff'))
    git_add_command = 'git add .'

    #ensure that runtime_dir (location where created/received files will go) exists, and if not, create it
    if os.path.exists(runtime_dir):
        if not os.path.isdir(runtime_dir): #diffs_path exists but is a file instead of a directory.  Delete it, and a make a directory in its place
            os.remove(runtime_dir)
            os.mkdir(runtime_dir)
    else:
        os.mkdir(runtime_dir)
    #End of all first-run initialization


    # Try to find saved server info (ip and various ports) in serverinfo.txt.
    serverinfo_path = os.path.join(runtime_dir, 'serverinfo.txt')
    serverinfo_dict = {}
    if os.path.exists(serverinfo_path):
        with open(serverinfo_path, 'r', encoding='utf-8') as f:
            serverinfo_dict = json.load(f)
    else:
        serverinfo_dict = {}

    # If connection metadata is missing, do a non-pairing discovery pass to bootstrap IP/ports.
    required_conn_keys = ['server_ip', 'server_port', 'server_socket_port', 'file_socket_port']
    missing_conn = [key for key in required_conn_keys if key not in serverinfo_dict]
    if missing_conn:
        discovery_result = discover_server(require_pairing=False)
        if discovery_result is None:
            raise RuntimeError('Unable to discover server connection metadata over UDP.')
        server_ip_discovered, discovered_info = discovery_result
        serverinfo_dict['server_ip'] = server_ip_discovered
        serverinfo_dict['server_port'] = int(discovered_info['server_port'])
        serverinfo_dict['server_socket_port'] = int(discovered_info['server_socket_port'])
        serverinfo_dict['file_socket_port'] = int(discovered_info['file_socket_port'])

    # Ensure we have paired cert + shared secret; this requires active pairing window when missing.
    serverinfo_dict = _ensure_pairing_material(serverinfo_path, runtime_dir, existing_info=serverinfo_dict)
    _initialize_security_context(runtime_dir, serverinfo_dict)

    required_keys = required_conn_keys
    missing_keys = [key for key in required_keys if key not in serverinfo_dict]
    if missing_keys:
        raise KeyError(f'serverinfo.txt is missing required keys: {missing_keys}')

    server_ip = serverinfo_dict['server_ip']
    server_port = int(serverinfo_dict['server_port'])
    server_socket_port = int(serverinfo_dict['server_socket_port'])
    file_socket_port = int(serverinfo_dict['file_socket_port'])
    server_addr = (server_ip, server_port)
 

    update_gitignore()


    



    if len(sys.argv) > 1:
        arg = sys.argv[1]
    else:
        arg = 'build'


    if arg == 'build': #for now, just to allow for flexibility in testing
        xcodebuild_args = []
        if len(sys.argv) > 2:
            xcodebuild_args = sys.argv[2:]
        git_diff_filepath, changed_binary_paths = prepare_text_changes()
        resp:Response = start_build_job(server_addr, git_diff_filepath, changed_binary_paths, xcodebuild_args)
        json_obj = json.loads(resp.text)
        job_id = json_obj['job_id']
        build_log_str = wait_for_build_completion(server_addr, job_id, server_socket_port)
        # print('final build log')
        # print(build_log_str)
    elif 'sendchanges' in arg:
        resp:Response = send_current_changes(server_addr)
        print(resp)
    elif 'getchanges' in arg:
        success:bool = retrieve_current_changes(server_addr)
        if success:
            print('Successfully retrieved changes from the server')
        else:
            print('Failed to retrieve changes from the server')
    elif 'sync' in arg:
        sync_changes_with_server(server_addr)
    elif 'sendfiles' in arg:
        if len(sys.argv) > 2:
            send_files(server_addr, [os.path.join(os.getcwd(), name) for name in sys.argv[2:]])
    else:
        print(f'Invalid argument: {arg}')
    
