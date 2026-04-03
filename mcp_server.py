from ntpath import dirname
import os, subprocess, socket, json, struct, hashlib, time, re, secrets, ssl, hmac, pty, select
from flask import Flask, request, send_file, send_from_directory, jsonify, Request, Response
from threading import Thread, Lock
from functools import wraps
from typing import Optional
from werkzeug.utils import secure_filename
from mcp_utils import *
from environment_setup import _normalize_path_for_compare
import projects_context_manager as pcm
# from requests import Request

server_port = 8751
server_socket_port = 50271
discovery_socket_port = 9346
file_socket_port = 47283
interactive_wrapper_port = 40887
pairing_duration_s = 60
allowed_timestamp_skew_s = 120
nonce_ttl_s = 300
SECURITY_METADATA_FILENAME = 'security_metadata.json'

# establish several filesystem level global variables
# pcm.cwd = unix_path(os.getpcm.cwd())
cwd = pcm.cwd
project_name = get_project_name()
server_dir_name = get_runtime_dir_name()
server_dir_path = os.path.join(pcm.cwd, server_dir_name)
project_info_filename = 'projectinfo.txt'
project_info_filepath = os.path.join(server_dir_path, project_info_filename)
legacy_allowed_interactive_commands_filename = 'allowed_interactive_commands.txt'
allowed_interactive_commands_filename = 'allowed-interactive-commands.txt'
legacy_allowed_interactive_commands_path = os.path.join(server_dir_path, legacy_allowed_interactive_commands_filename)
allowed_interactive_commands_path = os.path.join(server_dir_path, allowed_interactive_commands_filename)
user_runtime_dir_path = get_user_runtime_dir_path()

PAIRING_EXPIRY_UNIX = 0
PAIRING_LOCK = Lock()
NONCE_CACHE: dict[str, int] = {}
NONCE_LOCK = Lock()
SERVER_TLS_CONTEXT: Optional[ssl.SSLContext] = None
DISCOVERY_STATUS = {
    'started_at_unix': 0,
    'last_request_at_unix': 0,
    'last_response_at_unix': 0,
    'last_error': '',
}
DISCOVERY_LOCK = Lock()
INTERACTIVE_SESSION_TTL_SECONDS = 120
SERVER_SECURITY_METADATA: dict = {}


def get_server_port() -> int:
    return server_port


def _secrets_base_dir() -> str:
    return os.path.join(get_user_runtime_dir_path(), '.secrets')


def _security_metadata_path() -> str:
    return os.path.join(_secrets_base_dir(), SECURITY_METADATA_FILENAME)


def get_tls_paths() -> tuple[str, str]:
    tls_dir = os.path.join(_secrets_base_dir(), 'tls')
    return os.path.join(tls_dir, 'cert.pem'), os.path.join(tls_dir, 'key.pem')


def _get_hmac_secret_path() -> str:
    auth_dir = os.path.join(_secrets_base_dir(), 'auth')
    return os.path.join(auth_dir, 'hmac_secret.txt')


def _generate_secret_key_hmac() -> str:
    return secrets.token_urlsafe(48)


def _generate_secret_pair(path: Optional[str] = None) -> tuple[str, str]:
    if not path:
        path = get_user_runtime_dir_path()
    print('Generating self-signed certificates for HTTPS...')
    proc = subprocess.run(
        [
            'openssl',
            'req',
            '-x509',
            '-newkey',
            'rsa:4096',
            '-keyout',
            'key.pem',
            '-out',
            'cert.pem',
            '-sha256',
            '-days',
            '365',
            '-nodes',
            '-subj',
            '/CN=localhost',
        ],
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        err_text = proc.stderr.decode(errors='replace') if proc.stderr else 'unknown error'
        raise RuntimeError(f'Failed to generate TLS keypair with openssl: {err_text}')
    return os.path.join(path, 'cert.pem'), os.path.join(path, 'key.pem')


def get_hmac_secret() -> str:
    secret_path = _get_hmac_secret_path()
    with open(secret_path, 'r', encoding='utf-8') as f:
        return f.read().strip()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _current_security_fingerprints() -> tuple[str, str]:
    cert_fingerprint = _sha256_hex(get_certificate())
    hmac_fingerprint = _sha256_hex(get_hmac_secret())
    return cert_fingerprint, hmac_fingerprint


def _load_security_metadata() -> dict:
    now = int(time.time())
    cert_fingerprint, hmac_fingerprint = _current_security_fingerprints()
    metadata_path = _security_metadata_path()
    existing = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    server_id = str(existing.get('server_id', '')).strip() or str(uuid4())
    try:
        security_epoch = int(existing.get('security_epoch', 0))
    except (TypeError, ValueError):
        security_epoch = 0
    if security_epoch < 1:
        security_epoch = 1

    existing_cert_fp = str(existing.get('cert_fingerprint_sha256', '')).strip()
    existing_hmac_fp = str(existing.get('hmac_fingerprint_sha256', '')).strip()
    if existing and (existing_cert_fp != cert_fingerprint or existing_hmac_fp != hmac_fingerprint):
        security_epoch += 1

    metadata = {
        'server_id': server_id,
        'security_epoch': security_epoch,
        'cert_fingerprint_sha256': cert_fingerprint,
        'hmac_fingerprint_sha256': hmac_fingerprint,
        'updated_at_unix': now,
    }
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f)
    return metadata


def get_server_security_metadata() -> dict:
    if not SERVER_SECURITY_METADATA:
        raise RuntimeError('Server security metadata not initialized')
    return dict(SERVER_SECURITY_METADATA)

allowed_interactive_commands: list[str] = []
executable_command_paths_dict: dict[str, str] = {}


def _normalize_allowed_interactive_commands(raw_commands) -> list[str]:
    if not isinstance(raw_commands, list):
        return []
    normalized = []
    seen = set()
    for cmd in raw_commands:
        if not isinstance(cmd, str):
            continue
        cleaned = cmd.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return list(sorted(normalized))


def _extract_allowed_commands_from_payload(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    # Support both keys to remain compatible with any existing file contents.
    raw = payload.get('allowed_interactive_commands', payload.get('allowed-interactive-commands', []))
    return _normalize_allowed_interactive_commands(raw)


def _load_allowed_interactive_commands_file(path: str) -> list[str]:
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        return []
    payload = json.loads(text)
    return _extract_allowed_commands_from_payload(payload)


def _ensure_allowed_interactive_commands_file() -> None:
    os.makedirs(server_dir_path, exist_ok=True)
    if os.path.exists(allowed_interactive_commands_path):
        return
    if os.path.exists(legacy_allowed_interactive_commands_path):
        commands = _load_allowed_interactive_commands_file(legacy_allowed_interactive_commands_path)
        save_allowed_interactive_commands(commands)
        return
    save_allowed_interactive_commands([])


def load_allowed_interactive_commands() -> list[str]:
    _ensure_allowed_interactive_commands_file()
    try:
        return _load_allowed_interactive_commands_file(allowed_interactive_commands_path)
    except Exception as e:
        print(f'Error trying to decode JSON in {allowed_interactive_commands_path}: {e}')
        return []


def save_allowed_interactive_commands(allowed_commands: list[str]) -> None:
    os.makedirs(server_dir_path, exist_ok=True)
    normalized = _normalize_allowed_interactive_commands(allowed_commands)
    with open(allowed_interactive_commands_path, 'w', encoding='utf-8') as f:
        payload = {'allowed_interactive_commands': normalized}
        f.write(json.dumps(payload))


def add_allowed_interactive_command(name: str, save_allowed_commands: bool = True) -> bool:
    global allowed_interactive_commands
    global executable_command_paths_dict
    cleaned = name.strip()
    if not cleaned:
        return False
    if cleaned in allowed_interactive_commands:
        if cleaned not in executable_command_paths_dict:
            executable_command_paths_dict.update(_find_executable_paths([cleaned]))
        return True
    exe_paths = _find_executable_paths([cleaned])
    exe_path = exe_paths.get(cleaned, '')
    if not exe_path:
        return False
    allowed_interactive_commands = list(sorted(allowed_interactive_commands + [cleaned]))
    executable_command_paths_dict[cleaned] = exe_path
    if save_allowed_commands:
        save_allowed_interactive_commands(allowed_interactive_commands)
    return True


def remove_allowed_interactive_command(name: str, save_allowed_commands: bool = True) -> bool:
    global allowed_interactive_commands
    global executable_command_paths_dict
    cleaned = name.strip()
    old_len = len(allowed_interactive_commands)
    allowed_interactive_commands = [cmd for cmd in allowed_interactive_commands if cmd != cleaned]
    executable_command_paths_dict.pop(cleaned, None)
    changed = len(allowed_interactive_commands) != old_len
    if save_allowed_commands:
        save_allowed_interactive_commands(allowed_interactive_commands)
    return changed


def _ensure_allowed_interactive_commands() -> None:
    global allowed_interactive_commands
    global executable_command_paths_dict
    allowed_interactive_commands = load_allowed_interactive_commands()
    executable_command_paths_dict = _find_executable_paths(allowed_interactive_commands)
    if not allowed_interactive_commands:
        print('Continuing execution, however allowed_interactive_commands is an empty list. No interactive commands will be allowed')




def bootstrap_security_state() -> None:
    cert_path, key_path = get_tls_paths()
    secret_path = _get_hmac_secret_path()
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    os.makedirs(os.path.dirname(secret_path), exist_ok=True)

    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        generated_cert, generated_key = _generate_secret_pair(os.path.dirname(cert_path))
        if generated_cert != cert_path:
            os.replace(generated_cert, cert_path)
        if generated_key != key_path:
            os.replace(generated_key, key_path)

    if not os.path.exists(secret_path):
        with open(secret_path, 'w', encoding='utf-8') as f:
            f.write(_generate_secret_key_hmac())

    global SERVER_SECURITY_METADATA
    SERVER_SECURITY_METADATA = _load_security_metadata()

    global SERVER_TLS_CONTEXT
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    SERVER_TLS_CONTEXT = ctx
    _ensure_allowed_interactive_commands()


def pairing_window_state() -> tuple[bool, int]:
    now = int(time.time())
    with PAIRING_LOCK:
        is_enabled = now < PAIRING_EXPIRY_UNIX
        expires_at = PAIRING_EXPIRY_UNIX
    return is_enabled, expires_at


def enable_pairing_window(duration_s: int = pairing_duration_s) -> int:
    expires_at = int(time.time()) + int(duration_s)
    global PAIRING_EXPIRY_UNIX
    with PAIRING_LOCK:
        PAIRING_EXPIRY_UNIX = expires_at
    return expires_at


def get_certificate() -> str:
    cert_path, _ = get_tls_paths()
    with open(cert_path, 'r', encoding='utf-8') as f:
        return f.read().strip()


def _request_target(path: str, query: str) -> str:
    if not query:
        return path
    return f'{path}?{query}'


def _body_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signature_payload(method: str, target: str, timestamp: str, nonce: str, body_sha256: str) -> str:
    return '\n'.join([method.upper(), target, timestamp, nonce, body_sha256])


def _prune_nonce_cache(now_unix: int) -> None:
    stale = []
    for nonce, seen_at in NONCE_CACHE.items():
        if now_unix - seen_at > nonce_ttl_s:
            stale.append(nonce)
    for nonce in stale:
        NONCE_CACHE.pop(nonce, None)


def _verify_hmac_request(req: Request) -> tuple[bool, int, str]:
    timestamp = req.headers.get('X-RXS-Timestamp', '').strip()
    signature = req.headers.get('X-RXS-Signature', '').strip()
    nonce = req.headers.get('X-RXS-Nonce', '').strip()
    if not timestamp or not signature or not nonce:
        return False, 401, 'missing_auth_headers'
    try:
        ts = int(timestamp)
    except ValueError:
        return False, 401, 'invalid_timestamp'
    now = int(time.time())
    if abs(now - ts) > allowed_timestamp_skew_s:
        return False, 401, 'timestamp_out_of_window'

    with NONCE_LOCK:
        _prune_nonce_cache(now)
        if nonce in NONCE_CACHE:
            return False, 403, 'replayed_nonce'
        NONCE_CACHE[nonce] = now

    body_bytes = req.get_data(cache=True) or b''
    target = _request_target(req.path, req.query_string.decode('utf-8', errors='replace'))
    payload = _signature_payload(req.method, target, timestamp, nonce, _body_sha256(body_bytes))
    expected = hmac.new(get_hmac_secret().encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, 403, 'invalid_signature'
    return True, 200, 'ok'


def require_auth(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        ok, status_code, error = _verify_hmac_request(request)
        if not ok:
            return jsonify({'ok': False, 'error': error}), status_code
        return fn(*args, **kwargs)

    return inner


def _build_socket_auth_payload(channel: str, session_id: str, timestamp: str, nonce: str) -> str:
    return '\n'.join([channel, session_id, timestamp, nonce])


def _verify_socket_handshake(
    conn: socket.socket,
    expected_channel: Optional[str] = None,
    expected_session_id: Optional[str] = None,
) -> tuple[str, str]:
    header, _ = _recv_frame(conn)
    if header.get('type') != 'AUTH':
        raise PermissionError('missing auth handshake')
    channel = str(header.get('channel', ''))
    session_id = str(header.get('session_id', ''))
    timestamp = str(header.get('timestamp', ''))
    nonce = str(header.get('nonce', ''))
    signature = str(header.get('signature', ''))
    if expected_channel is not None and channel != expected_channel:
        raise PermissionError('invalid auth handshake channel')
    if expected_session_id is not None and session_id != expected_session_id:
        raise PermissionError('invalid auth handshake session id')
    try:
        ts = int(timestamp)
    except ValueError as e:
        raise PermissionError('invalid auth handshake timestamp') from e
    now = int(time.time())
    if abs(now - ts) > allowed_timestamp_skew_s:
        raise PermissionError('stale auth handshake timestamp')

    with NONCE_LOCK:
        _prune_nonce_cache(now)
        if nonce in NONCE_CACHE:
            raise PermissionError('replayed auth handshake nonce')
        NONCE_CACHE[nonce] = now

    payload = _build_socket_auth_payload(channel, session_id, timestamp, nonce)
    expected = hmac.new(get_hmac_secret().encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PermissionError('invalid auth handshake signature')
    _send_frame(conn, {'type': 'AUTH_ACK', 'ok': True})
    return channel, session_id


def _wrap_server_tls_socket(conn: socket.socket) -> ssl.SSLSocket:
    if SERVER_TLS_CONTEXT is None:
        raise RuntimeError('Server TLS context is not initialized')
    return SERVER_TLS_CONTEXT.wrap_socket(conn, server_side=True)


def start_discovery_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(2.0)
    s.bind(('0.0.0.0', discovery_socket_port))
    with DISCOVERY_LOCK:
        DISCOVERY_STATUS['started_at_unix'] = int(time.time())
        DISCOVERY_STATUS['last_error'] = ''

    while True:
        try:
            msg_bytes, addr = s.recvfrom(64 * KB)
        except socket.timeout:
            continue
        except Exception as e:
            with DISCOVERY_LOCK:
                DISCOVERY_STATUS['last_error'] = f'recv_error:{e}'
            continue

        with DISCOVERY_LOCK:
            DISCOVERY_STATUS['last_request_at_unix'] = int(time.time())

        if not msg_bytes.startswith(b'RXS_DISCOVERY_REQ'):
            continue

        response_lines = [
            'RXS_SERVER_HERE',
            (
                f'ports|server_port:{server_port},server_socket_port:{server_socket_port},'
                f'file_socket_port:{file_socket_port},interactive_wrapper_port:{interactive_wrapper_port}'
            ),
        ]
        try:
            pairing_enabled, expires_at = pairing_window_state()
            if pairing_enabled:
                cert_one_line = get_certificate().replace('\n', '<nl>')
                security_meta = get_server_security_metadata()
                response_lines.append(f'certificate:{cert_one_line}')
                response_lines.append(f'secret_key:{get_hmac_secret()}')
                response_lines.append(f'pairing_expires_unix:{expires_at}')
                response_lines.append(f"server_id:{security_meta['server_id']}")
                response_lines.append(f"security_epoch:{security_meta['security_epoch']}")
                response_lines.append(f"cert_fingerprint_sha256:{security_meta['cert_fingerprint_sha256']}")
        except Exception as e:
            with DISCOVERY_LOCK:
                DISCOVERY_STATUS['last_error'] = f'build_response_error:{e}'

        try:
            s.sendto('\n'.join(response_lines).encode('utf-8', errors='replace'), addr)
            with DISCOVERY_LOCK:
                DISCOVERY_STATUS['last_response_at_unix'] = int(time.time())
        except Exception as e:
            with DISCOVERY_LOCK:
                DISCOVERY_STATUS['last_error'] = f'send_error:{e}'


def get_project_root_path_server(project_id='', project_name='') -> str:
    '''This is sort of stupid but whatever, the tech debt of this project sort of forced me into defining this function
        as a server-only version to deal with certain issuesj'''
    project_root_path = ''
    if project_id:
        project = pcm.projects_dict.get(project_id, '')
        if project:
            return project.get('project_root_path')
        else:
            raise ValueError(f'Project is empty')
    elif project_name:
        for _, project in pcm.projects_dict.items():
            if project.get('project_name', '') == project_name:
                return project.get('project_root_path', '')

    if pcm.current_project:
        return pcm.current_project.get('project_root_path', '')

    return ''


    
def _locate_which_executable():
    primary_candidates = ['/usr/bin', '/bin']
    for dir_path in primary_candidates:
        full_path = os.path.join(dir_path, 'which')
        if os.path.exists(full_path):
            return full_path
    PATH_paths = os.environ['PATH'].split(os.pathsep)
    for dir_path in PATH_paths:
        full_path = os.path.join(dir_path, 'which')
        if os.path.exists(full_path):
            return full_path
    raise FileNotFoundError('Unable to locate "which" executable in primary_candidates')



def _find_executable_paths(command_names) -> dict[str, str]:
    path_dict = {}
    which_executable_path = _locate_which_executable()
    for name in command_names:
        if name not in path_dict:
            proc = run_process([which_executable_path, name])
            if proc.returncode != 0:
                print(f'Error locating executable for command: {name}')
            if proc.stdout:
                resolved = proc.stdout.decode(errors='replace').strip().splitlines()
                if not resolved:
                    continue
                executable_path = resolved[0].strip()
                if executable_path and os.path.isfile(executable_path) and os.access(executable_path, os.X_OK):
                    path_dict[name] = executable_path
    return path_dict

def _ensure_server_dir():
    """Ensures the existence of the global .remote-xcode-server directory located by default in the user's home dir"""
    user_home_dir = get_user_home_dir()
    server_dir = os.path.join(user_home_dir, server_dir_name)
    if not os.path.exists(server_dir):
        os.makedirs(server_dir)
    elif not os.path.isdir(server_dir):
        os.remove(server_dir)
        os.makedirs(server_dir)

def _set_upload_folder(path):
    global UPLOAD_FOLDER
    if not os.path.exists(path):
        raise FileNotFoundError(f'Error, could not find new upload folder: {path}')
    UPLOAD_FOLDER = path
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    pcm.UPLOAD_FOLDER = UPLOAD_FOLDER

#ensures existence of global (not project-specific) .remote-xcode-server directory
_ensure_server_dir()
#bootstrap security state
bootstrap_security_state()
#initialize the project context manager
pcm.initialize()
# ensure current upload dir exists even on a brand-new install with no tracked project yet
ensure_directory_exists(pcm.UPLOAD_FOLDER)

# set up sockets for streaming xcode commands (server) and sending/receiving files (filesocket)
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('0.0.0.0', server_socket_port))
server.listen(1)
filesocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
filesocket.bind(('0.0.0.0', file_socket_port))
filesocket.listen(1)
interactive_wrapper_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
interactive_wrapper_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
interactive_wrapper_socket.bind(('0.0.0.0', interactive_wrapper_port))
interactive_wrapper_socket.listen(16)

# launch thread that reports back to client on server discovery broadcast requests
discovery_thread = Thread(target=start_discovery_listener, args=[], daemon=True)
discovery_thread.start()

update_gitignore()


#FIRST RUN SETUP

if pcm.current_project:
    project_root = pcm.current_project.get('project_root_path', '')
    if not project_root:
        #project root found in current_project dict, but it has no value
        if not 'project_root_path' in pcm.current_project:
            raise KeyError(f"Key 'project_root_path' not found in current_project: {pcm.current_project}'")
        else:
            raise ValueError(f"Value for key 'project_root_path'")

    pcm.UPLOAD_FOLDER = os.path.join(project_root, pcm.UPLOAD_FOLDER_NAME)

    if not _normalize_path_for_compare(pcm.cwd) == _normalize_path_for_compare(project_root):
        if os.path.exists(project_root):
            pcm.cwd = project_root
        pass




    #this was moved to not be top-level anymore, because this is project-specific, and should only run
    #if we actually know which project we are in, or at least that we are in a project at all
    #otherwise it is just creating a folder in a random location, which we want to avoid
    if not uploads_folder_exists(project_id=pcm.current_project['id']):
        os.makedirs(pcm.UPLOAD_FOLDER)
        # os.mkdir(os.path.join(pcm.current_project['project_root_path'], pcm.UPLOAD_FOLDER_NAME))
else:
    if not isinstance(pcm.current_project, dict):
        raise ValueError("Something has gone seriously wrong.  pcm.current_project isn't even a dict.  pcm.currentproject: {pcm.currentproject}")






if not os.path.exists(os.path.join(pcm.UPLOAD_FOLDER, project_info_filename)): #this means this is the first time the server is being run,
        with open(os.path.join(pcm.UPLOAD_FOLDER, project_info_filename), 'w') as f:
            config_lines = [f'project_root:{pcm.cwd}\n', f'project_name:{project_name}\n']
            for line in config_lines:
                if line[-1] in '\n\r':
                    f.write(line)
                else:
                    f.write(line + '\n')
            # f.writelines(config_lines)





JOBS = {}
app = Flask(__name__)
UPLOAD_FOLDER = ''
_set_upload_folder(pcm.UPLOAD_FOLDER)
TRANSFER_SESSIONS: dict[str, dict] = {}
OUTBOUND_TRANSFER_SESSIONS: dict[str, dict] = {}
SESSION_LOCK = Lock()
SESSION_TTL_SECONDS = 30 * 60
INTERACTIVE_SESSIONS: dict[str, dict] = {}
INTERACTIVE_LOCK = Lock()



def _close_interactive_session_resources(session: dict) -> None:
    master_fd = session.get('master_fd')
    if isinstance(master_fd, int):
        try:
            os.close(master_fd)
        except OSError:
            pass
    proc = session.get('proc')
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _cleanup_expired_interactive_sessions(now: Optional[float] = None) -> None:
    if now is None:
        now = time.time()
    stale_stream_ids: list[str] = []
    with INTERACTIVE_LOCK:
        for stream_id, session in INTERACTIVE_SESSIONS.items():
            if session.get('attached', False):
                continue
            created_at = float(session.get('created_at', now))
            if now - created_at > INTERACTIVE_SESSION_TTL_SECONDS:
                stale_stream_ids.append(stream_id)
        stale_sessions = [INTERACTIVE_SESSIONS.pop(stream_id, None) for stream_id in stale_stream_ids]
    for session in stale_sessions:
        if session:
            _close_interactive_session_resources(session)


def _stream_interactive_session(conn: socket.socket, session: dict, stream_id: str) -> None:
    try:
        master_fd = int(session['master_fd'])
        proc = session['proc']
        while True:
            if proc.poll() is not None:
                # Process has exited; continue draining PTY output until EOF.
                pass
            r, _, _ = select.select([conn, master_fd], [], [], 1.0)
            if conn in r:
                user_input = conn.recv(1*KB)
                if not user_input:
                    break
                try:
                    os.write(master_fd, user_input)
                except OSError:
                    break
            if master_fd in r:
                try:
                    process_output = os.read(master_fd, 4*KB)
                except OSError:
                    break
                if not process_output:
                    break
                conn.sendall(process_output)
    except Exception as e:
        print(f'Error streaming interactive session {stream_id}: {e}')
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with INTERACTIVE_LOCK:
            final_session = INTERACTIVE_SESSIONS.pop(stream_id, None)
        if final_session:
            _close_interactive_session_resources(final_session)
    


def start_interactive_session_listener():
    while True:
        _cleanup_expired_interactive_sessions()
        try:
            raw_conn, addr = interactive_wrapper_socket.accept()
        except Exception as e:
            print(f'Error accepting interactive wrapper socket: {e}')
            continue

        remote_ip = addr[0] if addr else ''
        try:
            conn = _wrap_server_tls_socket(raw_conn)
        except Exception as e:
            print(f'Error establishing TLS for interactive socket: {e}')
            try:
                raw_conn.close()
            except Exception:
                pass
            continue

        try:
            _, stream_id = _verify_socket_handshake(conn, expected_channel='remote_executable')
        except Exception as e:
            print(f'Rejected interactive socket handshake: {e}')
            try:
                conn.close()
            except Exception:
                pass
            continue

        with INTERACTIVE_LOCK:
            session = INTERACTIVE_SESSIONS.get(stream_id)
            if not session:
                session_ok = False
                reject_reason = f'unknown stream id {stream_id}'
            elif session.get('attached', False):
                session_ok = False
                reject_reason = f'stream id {stream_id} already attached'
            elif session.get('client_ip', '') != remote_ip:
                session_ok = False
                reject_reason = (
                    f'IP mismatch for stream id {stream_id}: expected {session.get("client_ip", "")}, got {remote_ip}'
                )
            else:
                session_ok = True
                session['attached'] = True
                session['attached_at'] = time.time()
                reject_reason = ''

        if not session_ok:
            print(f'Rejected interactive socket connection: {reject_reason}')
            try:
                conn.close()
            except Exception:
                pass
            continue

        worker = Thread(target=_stream_interactive_session, args=(conn, session, stream_id), daemon=True)
        worker.start()


def _launch_interactive_wrapper(project_name: str, executable_name: str):
    if _looks_like_path_arg(executable_name):
        return jsonify({'ok': False, 'error': 'invalid_executable_name'}), 400
    if executable_name not in allowed_interactive_commands:
        return jsonify({'ok': False, 'error': 'command_not_allowed'}), 403

    command_path = executable_command_paths_dict.get(executable_name, '')
    if not command_path:
        return jsonify({'ok': False, 'error': 'command_not_found'}), 400

    slave_fd = None
    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            [command_path],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env={**os.environ, 'TERM': 'xterm-256color'},
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed_to_launch:{e}'}), 500
    finally:
        if isinstance(slave_fd, int):
            try:
                os.close(slave_fd)
            except Exception:
                pass

    stream_id = secrets.token_urlsafe(24)
    with INTERACTIVE_LOCK:
        INTERACTIVE_SESSIONS[stream_id] = {
            'stream_id': stream_id,
            'client_ip': request.remote_addr or '',
            'executable': executable_name,
            'master_fd': master_fd,
            'proc': proc,
            'created_at': time.time(),
            'attached': False,
        }

    return jsonify(
        {
            'ok': True,
            'stream_id': stream_id,
            'interactive_wrapper_port': interactive_wrapper_port,
            'executable': executable_name,
        }
    )


@app.route('/interactive/launch/<executable_name>', methods=['POST'])
def launch_interactive_wrapper_v2(executable_name: str):
    project_name = request.args.get('project_name', '')
    return _launch_interactive_wrapper(project_name, executable_name)


@app.route('/stream_interactive_wrapper/<executable_name>', methods=['GET'])
def launch_interactive_wrapper_legacy(executable_name: str):
    project_name = request.args.get('project_name', '')
    return _launch_interactive_wrapper(project_name, executable_name)


def _global_auth_gate():
    if request.path in ['/', '/enable_pairing', '/pairing-bootstrap', '/discovery-status']:
        return None
    ok, status_code, error = _verify_hmac_request(request)
    if not ok:
        return jsonify({'ok': False, 'error': error}), status_code
    return None


@app.before_request
def _before_request_func():
    auth_gate_res = _global_auth_gate()
    if auth_gate_res:
        return auth_gate_res
    project_nonspecific_routes = [
        '/enable_pairing',
        '/pairing-bootstrap',
        '/discovery-status',
        '/add_allowed_interactive_command',
        '/remove_allowed_interactive_command',
        '/get_allowed_interactive_commands',
        '/checkprogress/',
        '/status/',
    ]
    route_is_specific = not any([request.path.startswith(pathroute) for pathroute in project_nonspecific_routes])
    if route_is_specific:
        projects_context_result = pcm.handle_project_context()
        if projects_context_result:
            return jsonify({'ok': False, 'error': projects_context_result}), 400
        _set_upload_folder(pcm.UPLOAD_FOLDER)
    return None



def get_safe_project_path(client_path:str) -> str:
    """Resolve a client-supplied repo-relative path safely under the server project root."""
    if client_path is None:
        raise ValueError('Path is None')

    rel_path = unix_path(str(client_path)).strip()
    if rel_path == '':
        raise ValueError('Path is empty')
    if '\x00' in rel_path:
        raise ValueError('Path contains NUL byte')
    if rel_path.startswith('/'):
        raise ValueError(f'Absolute paths are not allowed: {rel_path}')

    # Reject Windows drive-letter paths (e.g. C:/foo) and UNC-like inputs.
    first_part = rel_path.split('/')[0]
    if len(first_part) >= 2 and first_part[1] == ':':
        raise ValueError(f'Drive-letter paths are not allowed: {rel_path}')
    if rel_path.startswith('//'):
        raise ValueError(f'UNC-like paths are not allowed: {rel_path}')

    normalized_rel = unix_path(os.path.normpath(rel_path))
    normalized_parts = [p for p in normalized_rel.split('/') if p not in ['', '.']]
    if any(p == '..' for p in normalized_parts):
        raise ValueError(f'Path traversal is not allowed: {rel_path}')

    project_root_abs = os.path.abspath(pcm.cwd)
    dest_abs = os.path.abspath(os.path.join(project_root_abs, normalized_rel))
    if os.path.commonpath([project_root_abs, dest_abs]) != project_root_abs:
        raise ValueError(f'Path escapes project root: {rel_path}')

    return dest_abs


def _cleanup_transfer_sessions() -> None:
    now = time.time()
    with SESSION_LOCK:
        stale_ids = [
            transfer_id
            for transfer_id, session in TRANSFER_SESSIONS.items()
            if now - session.get('last_updated', now) > SESSION_TTL_SECONDS
        ]
        for transfer_id in stale_ids:
            TRANSFER_SESSIONS.pop(transfer_id, None)
        stale_outbound_ids = [
            transfer_id
            for transfer_id, session in OUTBOUND_TRANSFER_SESSIONS.items()
            if now - session.get('last_updated', now) > SESSION_TTL_SECONDS
        ]
        for transfer_id in stale_outbound_ids:
            OUTBOUND_TRANSFER_SESSIONS.pop(transfer_id, None)


def _has_active_file_transfer() -> bool:
    with SESSION_LOCK:
        inbound_active = any(
            session.get('status') in ['initialized', 'awaiting_socket', 'receiving']
            for session in TRANSFER_SESSIONS.values()
        )
        outbound_active = any(
            session.get('status') in ['initialized', 'awaiting_socket', 'sending']
            for session in OUTBOUND_TRANSFER_SESSIONS.values()
        )
    return inbound_active or outbound_active


def _recv_exact(conn: socket.socket, num_bytes: int) -> bytes:
    data = bytearray()
    while len(data) < num_bytes:
        chunk = conn.recv(num_bytes - len(data))
        if not chunk:
            raise ConnectionError('Socket closed while reading frame')
        data.extend(chunk)
    return bytes(data)


def _send_frame(conn: socket.socket, header: dict, payload: bytes = b'') -> None:
    header_bytes = json.dumps(header).encode('utf-8')
    conn.sendall(struct.pack('!I', len(header_bytes)))
    conn.sendall(header_bytes)
    conn.sendall(struct.pack('!I', len(payload)))
    if payload:
        conn.sendall(payload)


def _recv_frame(conn: socket.socket) -> tuple[dict, bytes]:
    header_len = struct.unpack('!I', _recv_exact(conn, 4))[0]
    header = json.loads(_recv_exact(conn, header_len).decode('utf-8'))
    payload_len = struct.unpack('!I', _recv_exact(conn, 4))[0]
    payload = _recv_exact(conn, payload_len) if payload_len else b''
    return header, payload


interactive_session_thread = Thread(target=start_interactive_session_listener, args=[], daemon=True)
interactive_session_thread.start()


def _update_session(transfer_id: str, **updates) -> None:
    with SESSION_LOCK:
        session = TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return
        session.update(updates)
        session['last_updated'] = time.time()


def _append_session_error(transfer_id: str, message: str) -> None:
    with SESSION_LOCK:
        session = TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return
        session.setdefault('errors', []).append(message)
        session['status'] = 'error'
        session['last_updated'] = time.time()


def _append_outbound_session_error(transfer_id: str, message: str) -> None:
    with SESSION_LOCK:
        session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return
        session.setdefault('errors', []).append(message)
        session['status'] = 'error'
        session['last_updated'] = time.time()


def _file_sha256(path: str, chunk_size: int = 256 * KB) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _handle_file_transfer_session(transfer_id: str) -> None:
    current_file = None
    conn = None
    try:
        _update_session(transfer_id, status='awaiting_socket')
        raw_conn, _ = filesocket.accept()
        conn = _wrap_server_tls_socket(raw_conn)
        conn.settimeout(60)
        _verify_socket_handshake(conn, expected_channel='file_upload', expected_session_id=transfer_id)
        _update_session(transfer_id, status='receiving')

        while True:
            header, payload = _recv_frame(conn)
            msg_type = header.get('type', '')
            incoming_transfer_id = header.get('transfer_id', '')

            if incoming_transfer_id != transfer_id:
                _append_session_error(transfer_id, f'Unexpected transfer_id: {incoming_transfer_id}')
                _send_frame(conn, {'type': 'ERROR', 'ok': False, 'error': 'transfer_id mismatch'})
                return

            if msg_type == 'FILE_START':
                rel_path = header.get('rel_path', '')
                with SESSION_LOCK:
                    session = TRANSFER_SESSIONS.get(transfer_id, {})
                    expected_map = session.get('expected', {})
                    expected_meta = expected_map.get(rel_path)
                if not expected_meta:
                    _append_session_error(transfer_id, f'FILE_START for unknown path: {rel_path}')
                    _send_frame(conn, {'type': 'ACK_FILE_START', 'ok': False, 'rel_path': rel_path})
                    continue

                try:
                    destination_path = get_safe_project_path(rel_path)
                except ValueError as e:
                    _append_session_error(transfer_id, f'Invalid path for FILE_START {rel_path}: {e}')
                    _send_frame(conn, {'type': 'ACK_FILE_START', 'ok': False, 'rel_path': rel_path})
                    continue

                parent_dir = os.path.dirname(destination_path)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)
                temp_path = destination_path + '.part'
                if os.path.exists(temp_path):
                    os.remove(temp_path)

                current_file = {
                    'rel_path': rel_path,
                    'destination_path': destination_path,
                    'temp_path': temp_path,
                    'expected_size': int(expected_meta['size']),
                    'expected_sha256': expected_meta['sha256'],
                    'bytes_received': 0,
                    'hash': hashlib.sha256(),
                    'handle': open(temp_path, 'wb'),
                }
                _send_frame(conn, {'type': 'ACK_FILE_START', 'ok': True, 'rel_path': rel_path})

            elif msg_type == 'FILE_CHUNK':
                if not current_file:
                    _append_session_error(transfer_id, 'Received FILE_CHUNK without FILE_START')
                    continue
                current_file['handle'].write(payload)
                current_file['hash'].update(payload)
                current_file['bytes_received'] += len(payload)

            elif msg_type == 'FILE_END':
                rel_path = header.get('rel_path', '')
                if not current_file or rel_path != current_file['rel_path']:
                    _append_session_error(transfer_id, f'FILE_END mismatch for rel_path={rel_path}')
                    _send_frame(conn, {'type': 'FILE_RESULT', 'ok': False, 'rel_path': rel_path})
                    continue

                current_file['handle'].close()
                actual_size = current_file['bytes_received']
                actual_sha256 = current_file['hash'].hexdigest()
                expected_size = current_file['expected_size']
                expected_sha256 = current_file['expected_sha256']
                verified = actual_size == expected_size and actual_sha256 == expected_sha256
                rel_path = current_file['rel_path']

                if verified:
                    os.replace(current_file['temp_path'], current_file['destination_path'])
                else:
                    if os.path.exists(current_file['temp_path']):
                        os.remove(current_file['temp_path'])
                    _append_session_error(
                        transfer_id,
                        f'Integrity check failed for {rel_path}. expected_size={expected_size}, '
                        f'actual_size={actual_size}, expected_sha256={expected_sha256}, actual_sha256={actual_sha256}',
                    )

                with SESSION_LOCK:
                    session = TRANSFER_SESSIONS.get(transfer_id)
                    if session is not None:
                        session['received'][rel_path] = {
                            'rel_path': rel_path,
                            'size': actual_size,
                            'sha256': actual_sha256,
                            'verified': verified,
                        }
                        session['last_updated'] = time.time()
                _send_frame(conn, {'type': 'FILE_RESULT', 'ok': verified, 'rel_path': rel_path})
                current_file = None

            elif msg_type == 'TRANSFER_END':
                with SESSION_LOCK:
                    session = TRANSFER_SESSIONS.get(transfer_id, {})
                    expected_paths = set(session.get('expected', {}).keys())
                    received = session.get('received', {})
                    received_verified_paths = {
                        path for path, meta in received.items() if meta.get('verified', False)
                    }
                    missing_paths = sorted(expected_paths - received_verified_paths)
                if missing_paths:
                    _append_session_error(transfer_id, f'Missing or unverified files: {missing_paths}')
                    _send_frame(conn, {'type': 'TRANSFER_RECEIVED', 'ok': False, 'missing': missing_paths})
                else:
                    _update_session(transfer_id, status='received')
                    _send_frame(conn, {'type': 'TRANSFER_RECEIVED', 'ok': True})
                return

            else:
                _append_session_error(transfer_id, f'Unknown frame type: {msg_type}')

    except Exception as e:
        _append_session_error(transfer_id, f'Socket transfer exception: {e}')
    finally:
        if current_file and current_file.get('handle') and not current_file['handle'].closed:
            current_file['handle'].close()
        if current_file and current_file.get('temp_path') and os.path.exists(current_file['temp_path']):
            os.remove(current_file['temp_path'])
        if conn:
            conn.close()


def send_files_from_server(transfer_id: str) -> None:
    conn = None
    try:
        with SESSION_LOCK:
            session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return

        with SESSION_LOCK:
            session['status'] = 'awaiting_socket'
            session['last_updated'] = time.time()

        raw_conn, _ = filesocket.accept()
        conn = _wrap_server_tls_socket(raw_conn)
        conn.settimeout(60)
        _verify_socket_handshake(conn, expected_channel='file_download', expected_session_id=transfer_id)
        with SESSION_LOCK:
            session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
            if not session:
                return
            session['status'] = 'sending'
            session['last_updated'] = time.time()
            expected = session.get('expected', {})
            chunk_size = int(session.get('chunk_size', 64 * KB))

        for rel_path, meta in expected.items():
            full_path = meta['full_path']
            expected_size = int(meta['size'])
            expected_sha256 = meta['sha256']
            _send_frame(
                conn,
                {
                    'type': 'FILE_START',
                    'transfer_id': transfer_id,
                    'rel_path': rel_path,
                    'size': expected_size,
                    'sha256': expected_sha256,
                },
            )
            ack_header, _ = _recv_frame(conn)
            if ack_header.get('type') != 'ACK_FILE_START' or not ack_header.get('ok', False):
                _append_outbound_session_error(transfer_id, f'Client rejected FILE_START for {rel_path}: {ack_header}')
                return

            bytes_sent = 0
            with open(full_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    _send_frame(conn, {'type': 'FILE_CHUNK', 'transfer_id': transfer_id, 'rel_path': rel_path}, chunk)
                    bytes_sent += len(chunk)
            _send_frame(conn, {'type': 'FILE_END', 'transfer_id': transfer_id, 'rel_path': rel_path})

            file_result, _ = _recv_frame(conn)
            if file_result.get('type') != 'FILE_RESULT' or not file_result.get('ok', False):
                _append_outbound_session_error(transfer_id, f'Client file verify failed for {rel_path}: {file_result}')
                return
            if bytes_sent != expected_size:
                _append_outbound_session_error(
                    transfer_id,
                    f'Bytes sent mismatch for {rel_path}. expected={expected_size}, sent={bytes_sent}',
                )
                return
            with SESSION_LOCK:
                session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
                if session is not None:
                    session['sent'][rel_path] = {
                        'rel_path': rel_path,
                        'size': bytes_sent,
                        'sha256': expected_sha256,
                        'verified': True,
                    }
                    session['last_updated'] = time.time()

        _send_frame(conn, {'type': 'TRANSFER_END', 'transfer_id': transfer_id})
        transfer_ack, _ = _recv_frame(conn)
        if transfer_ack.get('type') != 'TRANSFER_RECEIVED' or not transfer_ack.get('ok', False):
            _append_outbound_session_error(transfer_id, f'Client transfer completion failed: {transfer_ack}')
            return

        with SESSION_LOCK:
            session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
            if session is not None:
                session['status'] = 'sent'
                session['last_updated'] = time.time()
    except Exception as e:
        _append_outbound_session_error(transfer_id, f'Outbound transfer exception: {e}')
    finally:
        if conn:
            conn.close()

_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/].+")
def _looks_like_path_arg(value: str):
    if not value:
        return False
    s = value.strip()
    if not s:
        return False
    return (
        s.startswith(('/', './', '../', '~', '~/'))
        or '\\' in s
        or _DRIVE_PATH_RE.match(s) is not None
    )

def _get_invalid_xcodebuild_args(args:list[str]) -> list[str]:
    '''Returns a list of invalid arguments'''
    project_root = get_project_root_path_server()
    path_bearing_flags = ['-project', '-workspace', '-xcconfig', '-sdk', '-derivedDataPath', '-resultBundlePath', '-resultStreamPath',
                          '-archivePath', '-exportPath', '-exportOptionsPlist', '-localizationPath', '-xctestrun', '-testProductsPath',
                          '-clonedSourcePackagesDirPath', '-packageCachePath', '-authenticationKeyPath', '-framework', '-library', '-headers', '-output']
    
    flags:list[tuple[int, str]] = []
    invalid_args:list[str] = []
    #establish flags and their indices within args
    for i, arg in enumerate(args):
        if len(arg) > 0:
            if arg[0] == '-':
                flags.append((i, arg))
    
    #we need indices so that we can check the "next argument" in the case of "-flag arg" syntax, since arg is the "next argument" in that situation
    for i, arg in flags:
        #handle "-flag=value" syntax
        if '=' in arg:
            arg_parts = arg.split('=', 1)
            if len(arg_parts) != 2:
                invalid_args.append(arg)
            else:
                flag_name, path = arg_parts
                if flag_name in path_bearing_flags:
                    #handle special -sdk case, since its value can be either a path or not a path, so it needs special handling
                    if flag_name == '-sdk':
                        if _looks_like_path_arg(path) and not is_subdir(path, project_root):
                            invalid_args.append(arg)
                    #handle regular case where flag_name is not -sdk
                    elif not is_subdir(path, project_root):
                        invalid_args.append(arg)
        #handle the "-flag value" syntax
        else:
            flag_name = arg
            if i+1 < len(args):
                path = args[i+1]
                if flag_name in path_bearing_flags:
                    if flag_name == '-sdk':
                        if _looks_like_path_arg(path) and not is_subdir(path, project_root):
                            invalid_args.append(arg)
                    elif not is_subdir(path, project_root):
                        invalid_args.append(arg)
            else:
                #since this is specifically the "-flag value" syntax, not having a next arg means that the flag has no value.  However
                #there are some flags that ARE just a flag with no additional value, so this does not inherently prove the flag is invalid
                #but it's likely enough that I felt like I should create this else statement and leave this comment here at least
                pass

    return invalid_args


def run_xcodebuild(job_id, xcodebuild_args):
    conn = None
    proc = None
    log_write = None
    try:
        raw_conn, addr = server.accept()
        conn = _wrap_server_tls_socket(raw_conn)
        print(f'Received connection from {addr}')
        _verify_socket_handshake(conn, expected_channel='build_log', expected_session_id=str(job_id))
        job = JOBS[job_id]
        log_write = job['file']
        invalid_args = _get_invalid_xcodebuild_args(xcodebuild_args)
        if invalid_args:
            msg = f'Error (invalid args): '
            for arg in invalid_args[:-1]:
                msg += f'"{arg}", '
            msg += invalid_args[-1]
            job['status'] = 'error'
            job['error'] = msg
            conn.sendall(msg.encode())
            return

        job['status'] = 'running'
        xcodebuild_command: list[str] = ['xcodebuild', *xcodebuild_args]
        proc = subprocess.Popen(xcodebuild_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, shell=False)
        chunk_size = 4096
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            log_write.write(chunk.decode(errors='replace'))
            conn.sendall(chunk)

        return_code = proc.wait()
        if return_code == 0:
            job['status'] = 'done'
        else:
            job['status'] = 'error'
            job['error'] = f'xcodebuild exited with return code {return_code}'
    except Exception as e:
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)
    finally:
        if log_write and not log_write.closed:
            log_write.close()
        if conn:
            conn.close()
        

@app.route('/enable_pairing')
def enable_pairing():
    expires_at = enable_pairing_window(pairing_duration_s)
    return jsonify({'ok': True, 'pairing_enabled': True, 'expires_at_unix': expires_at})


@app.route('/pairing-bootstrap')
def pairing_bootstrap():
    pairing_enabled, expires_at = pairing_window_state()
    if not pairing_enabled:
        return jsonify({'ok': False, 'error': 'pairing_disabled_or_expired'}), 403
    cert_one_line = get_certificate().replace('\n', '<nl>')
    security_meta = get_server_security_metadata()
    return jsonify(
        {
            'ok': True,
            'pairing_enabled': True,
            'expires_at_unix': expires_at,
            'server_port': server_port,
            'server_socket_port': server_socket_port,
            'file_socket_port': file_socket_port,
            'interactive_wrapper_port': interactive_wrapper_port,
            'certificate': cert_one_line,
            'secret_key': get_hmac_secret(),
            'server_id': security_meta['server_id'],
            'security_epoch': security_meta['security_epoch'],
            'cert_fingerprint_sha256': security_meta['cert_fingerprint_sha256'],
        }
    )


@app.route('/discovery-status')
def discovery_status():
    security_meta = get_server_security_metadata()
    with DISCOVERY_LOCK:
        return jsonify(
            {
                'ok': True,
                'server_port': server_port,
                'server_socket_port': server_socket_port,
                'file_socket_port': file_socket_port,
                'interactive_wrapper_port': interactive_wrapper_port,
                'server_id': security_meta['server_id'],
                'security_epoch': security_meta['security_epoch'],
                'cert_fingerprint_sha256': security_meta['cert_fingerprint_sha256'],
                **DISCOVERY_STATUS,
            }
        )

@app.route('/add_allowed_interactive_command/<command>')
def add_allowed_interactive_command_route(command:str):
    command = command.strip()
    if ' ' in command:
        return jsonify({'ok': False, 'error': f'Invalid command: {command}. Commands cannot contain spaces'}), 400
    elif _looks_like_path_arg(command):
        return jsonify({'ok': False, 'error': f'Invalid command: {command}. Commands cannot be paths'}), 400
    elif command in allowed_interactive_commands:
        if command not in executable_command_paths_dict:
            executable_command_paths_dict.update(_find_executable_paths([command]))
        return jsonify({'ok': True, 'allowed_interactive_commands': allowed_interactive_commands}), 200

    _exe_paths = _find_executable_paths([command])
    exe_path = _exe_paths.get(command)
    if exe_path:
        if not (os.path.exists(exe_path) and os.path.isfile(exe_path) and os.access(exe_path, os.X_OK)):
            error_msg = f'Error, detected path for command: {command} (somehow) does not exist'
            print(error_msg)
            return jsonify({'ok': False, 'error': error_msg}), 500
    else:
        error_msg = f'Error, unable to find executable for command: {command}'
        print(error_msg)
        return jsonify({'ok': False, 'error': error_msg}), 400

    #actually add the command to list of interactive commands, and save it permanently in a file
    if not add_allowed_interactive_command(command):
        return jsonify({'ok': False, 'error': f'Failed to add interactive command: {command}'}), 500
    return jsonify({'ok': True, 'allowed_interactive_commands': allowed_interactive_commands}), 200


@app.route('/remove_allowed_interactive_command/<command>')
def remove_allowed_interactive_command_route(command:str):
    command = command.strip()
    remove_allowed_interactive_command(command)
    return jsonify({'ok': True, 'allowed_interactive_commands': allowed_interactive_commands}), 200


@app.route('/get_allowed_interactive_commands')
def get_allowed_interactive_commands_route():
    return jsonify({'ok': True, 'allowed_interactive_commands': allowed_interactive_commands}), 200



@app.route('/retrieve_text_changes')
def send_changes():
    project_name = request.args.get('project_name', '')
    git_diff_path, changed_binary_paths = prepare_text_changes()
    git_diff_dir, git_diff_name = [unix_path(p) for p in os.path.split(git_diff_path)]
    return send_from_directory(git_diff_dir, git_diff_name, as_attachment=True)

@app.route('/retrieve_changed_binary_paths')
def send_changed_binary_paths():
    project_name = request.args.get('project_name', '')
    changed_file_paths = [path for path in get_changed_file_paths() if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    paths_str = '\n'.join(changed_binary_paths)
    return paths_str

@app.route('/retrieve_diff_for_files', methods=['POST'])
def send_diff_for_files():
    project_name = request.args.get('project_name', '')
    try:
        paths = request.json['filepaths']
    except KeyError as e:
        print(f'Error: key filepaths not found in request json.  err: {e}')

    git_diff_path = get_diff_for_files(paths)
    diffs_path, git_diff_name = os.path.split(git_diff_path)
    return send_from_directory(diffs_path, git_diff_name)


@app.route('/retrieve_current_commit_hash')
def send_current_commit_hash():
    project_name = request.args.get('project_name', '')
    current_commit_hash = get_current_commit_hash()
    return current_commit_hash


@app.route('/retrieve_git_branches/<sort_order>')
def send_git_branches(sort_order:str):
    project_name = request.args.get('project_name', '')
    git_branches, current_branch = get_git_branches(project_name, return_current_branch=True, sort_order=sort_order)
    return jsonify({'branches': git_branches, 'current_branch': current_branch})


@app.route('/git_state')
def send_git_state():
    project_name = request.args.get('project_name', '')
    try:
        state = get_git_state(get_project_root_path_server(project_name=project_name))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify(state)


@app.route('/git_action', methods=['POST'])
def run_git_action():
    project_name = request.args.get('project_name', '')
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': 'Expected JSON object payload'}), 400

    allowed_payload_keys = {'action', 'args'}
    unknown_payload_keys = set(payload.keys()) - allowed_payload_keys
    if unknown_payload_keys:
        return jsonify({
            'success': False,
            'error': f'Unknown top-level payload keys: {sorted(unknown_payload_keys)}',
        }), 400

    action = payload.get('action', '')
    if not isinstance(action, str) or not action:
        return jsonify({'success': False, 'error': 'Field "action" is required and must be a string'}), 400

    action_args = payload.get('args', {})
    if action_args is None:
        action_args = {}
    if not isinstance(action_args, dict):
        return jsonify({'success': False, 'error': 'Field "args" must be an object if provided'}), 400

    result = execute_git_action(action, action_args, cwd=get_project_root_path_server(project_name=project_name))
    if 'command' not in result and not result.get('success', False):
        return jsonify(result), 400
    return jsonify(result)


@app.route('/create-update-bundle/<client_head>', methods=['GET'])
def send_update_bundle(client_head:str):
    project_name = request.args.get('project_name', '')
    runtime_dir_path = get_runtime_dir_path()
    bundle_name = 'update.bundle'
    save_path = os.path.join(runtime_dir_path, bundle_name)
    if not client_head:
        print(f'Error: {client_head} not found in request.values.keys')
        return f'Error: {client_head} not found in request.values.keys'
    git_create_update_bundle(client_head, 'HEAD', save_path)
    return save_path


#note that <path:path> is NOT representing <variablename:variablename>. The full syntax for route variables is <converter:name>
#so it is just a coincidence that the thing I'm trying to pass here as a variable in the url IS literally a path, which happens to be
#the same as the name of the converter we need to use: "path".  The path converter makes it so that we can pass nested paths here, instead
#of paths getting cut off once they reach the first '/' (the first path delimiter)
@app.route('/retrieve_binary_file/<path:path>')
def send_binary_file(path:str):
    project_name = request.args.get('project_name', '')
    path = get_safe_project_path(path)
    if not os.path.exists(path):
        filename = os.path.split(path)[-1]
        return f'Invalid path/requested file: {filename} does not exist'
    elif not os.path.isfile(path):
        filename = os.path.split(path)[-1]
        if os.path.isdir(path):
            return f'Path: {path} exists, but is a directory.  Only individual binary files can be requested via this route'
        else:
            return f'Path: {path} exists, but is somehow neither a regular file or directory.  You did something really crazy.'

    return send_file(path, as_attachment=True)


@app.route('/apply-patch-server', methods=['GET'])
def apply_patch_server():
    project_name = request.args.get('project_name', '')
    patch_path = request.args.get('patch_path', None)
    if not patch_path:
        print('Error: No patch path received from client')
        return 'Error: no patch path received from the client'
    apply_patch(patch_path)
    return 'successfully applied patch'


@app.route('/sendchanges', methods=['GET'])
def receive_changes():
    project_name = request.args.get('project_name', '')
    runtime_dir = get_runtime_dir_path()
    patch_path = os.path.join(runtime_dir, 'gitdiff.diff')
    if os.path.exists(patch_path) and (os.path.getsize(patch_path) > 0):
        git_apply_command = f'git apply {patch_path}'
        #run the git apply command
        os.system(git_apply_command)
    else:
        print('No diff file or empty diff file')
        return 'No diff file or empty diff file'
    return 'Successfully applied changes'



@app.route('/sendfileshttp', methods=['POST'])
def receive_files_http():
    project_name = request.args.get('project_name', '')
    saved_files = []
    errors = []
    for key in request.files.keys():
        file = request.files[key]
        if not file:
            errors.append(f'No file object for request.files key: {key}')
            continue
        rel_path = unix_path(str(file.filename))
        try:
            destination_path = get_safe_project_path(rel_path)
        except ValueError as e:
            errors.append(f'Invalid path {rel_path}: {e}')
            continue

        parent_dir = os.path.dirname(destination_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        if os.path.exists(destination_path) and os.path.isdir(destination_path):
            errors.append(f'Destination path exists as a directory: {rel_path}')
            continue

        file.save(destination_path)
        digest = hashlib.sha256()
        with open(destination_path, 'rb') as f:
            while True:
                chunk = f.read(256 * KB)
                if not chunk:
                    break
                digest.update(chunk)
        saved_files.append(
            {
                'rel_path': rel_path,
                'size': os.path.getsize(destination_path),
                'sha256': digest.hexdigest(),
                'verified': True,
            }
        )
    ok = len(errors) == 0
    status = 200 if ok else 400
    return jsonify({'ok': ok, 'received_files': saved_files, 'errors': errors}), status


@app.route('/sendfilessocket/init', methods=['POST'])
def init_receive_files_socket():
    project_name = request.args.get('project_name', '')
    _cleanup_transfer_sessions()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'errors': ['Expected JSON object payload']}), 400

    transfer_id = payload.get('transfer_id', '')
    files = payload.get('files', [])
    chunk_size = int(payload.get('chunk_size', 64 * KB))
    if not isinstance(transfer_id, str) or not transfer_id:
        return jsonify({'ok': False, 'errors': ['Field "transfer_id" is required and must be a string']}), 400
    if not isinstance(files, list) or not files:
        return jsonify({'ok': False, 'errors': ['Field "files" must be a non-empty list']}), 400

    expected = {}
    errors = []
    for entry in files:
        if not isinstance(entry, dict):
            errors.append('Each "files" entry must be an object')
            continue
        rel_path = entry.get('rel_path', '')
        size = entry.get('size', -1)
        sha256 = entry.get('sha256', '')
        if not isinstance(rel_path, str) or not rel_path:
            errors.append(f'Invalid rel_path: {rel_path}')
            continue
        try:
            get_safe_project_path(rel_path)
        except ValueError as e:
            errors.append(f'Invalid rel_path "{rel_path}": {e}')
            continue
        if not isinstance(size, int) or size < 0:
            errors.append(f'Invalid size for {rel_path}: {size}')
            continue
        if not isinstance(sha256, str) or len(sha256) != 64:
            errors.append(f'Invalid sha256 for {rel_path}')
            continue
        expected[rel_path] = {'size': size, 'sha256': sha256}

    if errors:
        return jsonify({'ok': False, 'transfer_id': transfer_id, 'errors': errors}), 400

    if _has_active_file_transfer():
        return jsonify({'ok': False, 'errors': ['Another file transfer is currently active']}), 409
    with SESSION_LOCK:
        TRANSFER_SESSIONS[transfer_id] = {
            'transfer_id': transfer_id,
            'status': 'initialized',
            'chunk_size': chunk_size,
            'expected': expected,
            'received': {},
            'errors': [],
            'created_at': time.time(),
            'last_updated': time.time(),
        }

    t = Thread(target=_handle_file_transfer_session, args=(transfer_id,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'transfer_id': transfer_id, 'file_socket_port': file_socket_port, 'errors': []})


@app.route('/sendfilesfromserver/init', methods=['POST'])
def init_send_files_from_server():
    project_name = request.args.get('project_name', '')
    _cleanup_transfer_sessions()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'errors': ['Expected JSON object payload']}), 400

    transfer_id = payload.get('transfer_id', '')
    paths = payload.get('paths', [])
    chunk_size = int(payload.get('chunk_size', 64 * KB))
    if not isinstance(transfer_id, str) or not transfer_id:
        return jsonify({'ok': False, 'errors': ['Field "transfer_id" is required and must be a string']}), 400
    if not isinstance(paths, list) or not paths:
        return jsonify({'ok': False, 'errors': ['Field "paths" must be a non-empty list']}), 400
    if _has_active_file_transfer():
        return jsonify({'ok': False, 'errors': ['Another file transfer is currently active']}), 409

    expected = {}
    errors = []
    for path in paths:
        if not isinstance(path, str) or not path:
            errors.append(f'Invalid path value: {path}')
            continue
        rel_path = unix_path(path)
        try:
            full_path = get_safe_project_path(rel_path)
        except ValueError as e:
            errors.append(f'Invalid rel_path "{rel_path}": {e}')
            continue
        if not os.path.exists(full_path) or not os.path.isfile(full_path):
            errors.append(f'Path does not exist as a regular file on server: {rel_path}')
            continue
        expected[rel_path] = {
            'full_path': full_path,
            'size': os.path.getsize(full_path),
            'sha256': _file_sha256(full_path),
        }
    if errors:
        return jsonify({'ok': False, 'transfer_id': transfer_id, 'errors': errors}), 400

    with SESSION_LOCK:
        OUTBOUND_TRANSFER_SESSIONS[transfer_id] = {
            'transfer_id': transfer_id,
            'status': 'initialized',
            'chunk_size': chunk_size,
            'expected': expected,
            'sent': {},
            'errors': [],
            'created_at': time.time(),
            'last_updated': time.time(),
        }
    t = Thread(target=send_files_from_server, args=(transfer_id,), daemon=True)
    t.start()
    manifest = [
        {'rel_path': rel_path, 'size': meta['size'], 'sha256': meta['sha256']}
        for rel_path, meta in expected.items()
    ]
    return jsonify(
        {
            'ok': True,
            'transfer_id': transfer_id,
            'file_socket_port': file_socket_port,
            'files': manifest,
            'errors': [],
        }
    )


@app.route('/sendfilesfromserver/complete', methods=['POST'])
def complete_send_files_from_server():
    project_name = request.args.get('project_name', '')
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'errors': ['Expected JSON object payload']}), 400
    transfer_id = payload.get('transfer_id', '')
    if not isinstance(transfer_id, str) or not transfer_id:
        return jsonify({'ok': False, 'errors': ['Field "transfer_id" is required and must be a string']}), 400

    wait_deadline = time.time() + 10
    while time.time() < wait_deadline:
        with SESSION_LOCK:
            session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
            status = session.get('status', '') if session else ''
        if not session:
            return jsonify({'ok': False, 'errors': [f'Unknown transfer_id: {transfer_id}']}), 404
        if status in ['sent', 'error']:
            break
        time.sleep(0.1)

    with SESSION_LOCK:
        session = OUTBOUND_TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return jsonify({'ok': False, 'errors': [f'Unknown transfer_id: {transfer_id}']}), 404
        expected_paths = set(session.get('expected', {}).keys())
        sent_map = session.get('sent', {})
        sent_paths = {path for path, meta in sent_map.items() if meta.get('verified', False)}
        missing_paths = sorted(expected_paths - sent_paths)
        errors = list(session.get('errors', []))
        if missing_paths:
            errors.append(f'Missing or unverified files: {missing_paths}')
        ok = len(errors) == 0 and len(sent_paths) == len(expected_paths)
        session['status'] = 'completed' if ok else 'error'
        session['last_updated'] = time.time()
        sent_files = list(sent_map.values())
    return jsonify({'ok': ok, 'sent_files': sent_files, 'errors': errors}), (200 if ok else 400)


@app.route('/sendfilessocket/complete', methods=['POST'])
def complete_receive_files_socket():
    project_name = request.args.get('project_name', '')
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'errors': ['Expected JSON object payload']}), 400
    transfer_id = payload.get('transfer_id', '')
    if not isinstance(transfer_id, str) or not transfer_id:
        return jsonify({'ok': False, 'errors': ['Field "transfer_id" is required and must be a string']}), 400

    wait_deadline = time.time() + 10
    while time.time() < wait_deadline:
        with SESSION_LOCK:
            session = TRANSFER_SESSIONS.get(transfer_id)
            status = session.get('status', '') if session else ''
        if not session:
            return jsonify({'ok': False, 'errors': [f'Unknown transfer_id: {transfer_id}']}), 404
        if status in ['received', 'error']:
            break
        time.sleep(0.1)

    with SESSION_LOCK:
        session = TRANSFER_SESSIONS.get(transfer_id)
        if not session:
            return jsonify({'ok': False, 'errors': [f'Unknown transfer_id: {transfer_id}']}), 404
        expected_paths = set(session.get('expected', {}).keys())
        received_map = session.get('received', {})
        verified_paths = {path for path, meta in received_map.items() if meta.get('verified', False)}
        missing_paths = sorted(expected_paths - verified_paths)
        errors = list(session.get('errors', []))
        if missing_paths:
            errors.append(f'Missing or unverified files: {missing_paths}')
        ok = len(errors) == 0 and len(verified_paths) == len(expected_paths)
        session['status'] = 'completed' if ok else 'error'
        session['last_updated'] = time.time()
        received_files = list(received_map.values())
    status_code = 200 if ok else 400
    return jsonify({'ok': ok, 'received_files': received_files, 'errors': errors}), status_code


@app.route('/sendfilessocket', methods=['POST'])
def receive_files_socket_legacy():
    project_name = request.args.get('project_name', '')
    try:
        data = request.get_json(silent=True) or {}
        paths = data.get('filepaths', [])
    except Exception:
        paths = []
    return jsonify(
        {
            'ok': False,
            'errors': ['Legacy route no longer supported. Use /sendfilessocket/init?project_name=<project_name> and /sendfilessocket/complete?project_name=<project_name>.'],
            'paths_seen': paths,
        }
    ), 410


@app.route('/start-build-job', methods=['POST'])
def start_build_job():
    project_name = request.args.get('project_name', '')
    print(f'project_name: {project_name}')
    xcodebuild_args = request.form.getlist('xcodebuild_args')

    if request.method == 'POST' or request.method == 'GET':
        if 'gitdiff' not in request.files:
            print('NO FILE PART')
            return 'NO FILE PART'

        file = request.files['gitdiff']
        print(f'file: {file}')
        print(f'file.filename: {file.filename}')
        print(f'file.name: {file.name}')
        if file == '':
            print('empty filename')
            return 'empty filename'

        print(f'request.files: {request.files}')

        #there are are additional file(s) besides the diff.  This means the client sent binary files
        #we need to save these files to their paths (path is the first item of the tuple)
        # for i in range(1, len(request.files.keys())):
        for file_key in request.files.keys():
            if file_key == 'gitdiff':
                continue
            binary_file = request.files[file_key]
            #I know this seems wrong.  But FileStorage.filename always returns the FIRST ITEM (0 index) in the tuple that was 
            #used as the value in the files dict sent by the requests library (from the client).  And for the binary files, I am
            #passing the path in the 0th index instead of the filename, because I need to save the files in the same relative locations
            rel_path = unix_path(binary_file.filename)
            binary_file_name = rel_path.split('/')[-1]
            path = get_safe_project_path(rel_path)
            parent_dir = os.path.dirname(path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            if os.path.exists(path):
                if os.path.isfile(path):
                    #If the path exists and IS a a file, simply remove it, since FileStorage.save does not overwrite files apparently
                    os.remove(path)
                else:
                    print(f'Path given for changed or added binary file {binary_file_name}: {path}, already exists as a directory')
                    print("Honestly, that's just really strange.  Idk")

            binary_file.save(path)



        if file and allowed_filename(file.filename):
            #create a secure version of the filename
            filename = secure_filename(file.filename)
            #save the file with the secure filename in UPLOAD_FOLDER
            file.save(os.path.join(pcm.UPLOAD_FOLDER, filename))

            patch_path = f'{pcm.UPLOAD_FOLDER}/{filename}'
            if os.path.getsize(patch_path) > 0:
                git_apply_command = f'git apply {patch_path}'
                #run the git apply command
                os.system(git_apply_command)


            job_id = str(uuid4())
            if job_id in JOBS.keys():
                return f'<p>Already building {project_name}, job_id: {job_id}</p>'

            build_log_name:str = f'buildlog-{job_id}.txt'
            build_log_path:str = os.path.join(pcm.UPLOAD_FOLDER, build_log_name)

            #Create the new job object and put it in job_id in the JOBS dict.
            #We have to be careful to ensure the file gets closed
            build_log_file = open(build_log_path, 'w')
            JOBS[job_id] = {"status": "pending", "result": '', "error": None, "file": build_log_file}

            t = Thread(target=run_xcodebuild, args=([job_id, xcodebuild_args]), daemon=True)
            t.start()
            return jsonify({"job_id": job_id}), 202
        else:
            return 'No file, or disallowed file type was uploaded'
    else:
        return "Some other method besides POST or GET was used.  Don't do that"
        

@app.route('/retrieve_changed_file_paths/<scope>')
def send_changed_file_paths(scope:str) -> Response:
    project_name = request.args.get('project_name', '')
    changed_file_paths = get_changed_file_paths(scope)
    changed_plaintext_paths = [path for path in changed_file_paths if is_plaintext(path)]
    changed_binary_paths = [path for path in changed_file_paths if path not in changed_plaintext_paths]
    obj = {'plaintext_file_paths': changed_plaintext_paths, 'binary_file_paths': changed_binary_paths}
    return jsonify(obj)



@app.route('/checkprogress/<job_id>/<offset>')
def check_progress(job_id:str, offset:int) -> Response:
    job = JOBS[job_id]
    if job['status'] == 'done':
        return 'Build already Complete'
    elif job['status'] == 'error':
        return f"Job status returned error.  Error message: {job['error']}"

    build_log_path = get_build_log_path(job_id)
    if not os.path.exists(build_log_path):
        return 'Error: build job does not exist.'
    with open(build_log_path, 'r') as f:
        if f.seekable():
            f.seek(int(offset), 0)
        else:
            f.read(offset)
        new_text = f.read()

    job['result'] += new_text
    return jsonify({'job_id': job_id, 'status': 'pending', 'newtext': new_text, 'result': job['result']})








@app.route('/status/<job_id>')
def job_status(job_id):
    if job_id not in JOBS:
        job_id = str(uuid4())
    job = JOBS.get(job_id)
    if not job:
        return jsonify("error", "job not found"), 404
    return jsonify(job)




@app.route('/')
def hello_world():
    return 'Hello, World!'



if __name__ == '__main__':
    ip, port = '0.0.0.0', get_server_port()
    cert_path, key_path = get_tls_paths()
    app.run(ip, port, ssl_context=(cert_path, key_path))


