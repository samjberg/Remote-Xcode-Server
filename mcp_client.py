import sys, os, socket, requests, json, urllib, hashlib, struct
from requests import Response
from mcp_utils import *

def configure_stdio():
    """Ensure redirected output can represent UTF-8 build logs on Windows."""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def get_jobid_from_resp(resp:Response):
    return json.loads(resp.text)['job_id']

def retrieve_file(server_addr:tuple[str, int], path) -> bool:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/retrieve_files/{app_name}/{path}'
    dct = {'paths': [path]}
    ran_successfully = True
    try:
        resp:Response = requests.get(url, dct)
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


def start_build_job(server_addr:tuple[str, int], git_diff_path:str, changed_binary_paths:list[str]=[]) -> str:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/appname/{app_name}'
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
    resp = requests.post(url, files=files)
    return resp

def check_build_job(server_addr:tuple[str, int], job_id:str, offset:int=0) -> Response:
    ip, port = server_addr
    url = f'http://{ip}:{port}/checkprogress/{job_id}/{offset}'
    resp = requests.get(url)
    return resp

def wait_for_build_completion(server_addr:tuple[str, int], job_id:str, offset=0) -> str:
    ip, port = server_addr
    server_socket_port = 50271
    chunk_size = 4096
    full_text = ''
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, server_socket_port))
        while True:
            new_bytes = s.recv(chunk_size)
            if not new_bytes:
                break
            new_text = new_bytes.decode('utf-8', errors='replace')
            print(new_text, end='')
            full_text += new_text

    return full_text


def _receive_file_over_socket(server_addr:tuple[str, int]) -> bool:
    print('Legacy _receive_file_over_socket is disabled. Use send_files() transfer APIs.')
    return False


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
    ip, port = server_addr
    app_name = get_appname()
    if isinstance(paths, str):
        paths = [paths]
    rel_paths = [unix_path(path) for path in paths]
    transfer_id = str(uuid4())
    init_url = f'http://{ip}:{port}/sendfilesfromserver/init/{app_name}'
    init_payload = {'transfer_id': transfer_id, 'paths': rel_paths, 'chunk_size': chunk_size}
    try:
        init_resp = requests.post(init_url, json=init_payload)
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
    sock_port = int(init_obj.get('file_socket_port', file_socket_port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, sock_port))
        s.settimeout(60)
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

    complete_url = f'http://{ip}:{port}/sendfilesfromserver/complete/{app_name}'
    try:
        complete_resp = requests.post(complete_url, json={'transfer_id': transfer_id})
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
    ip, port = server_addr
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
        url = f'http://{ip}:{port}/sendfileshttp/{app_name}'
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

            resp = requests.post(url, files=files)
        finally:
            for handle in handles:
                handle.close()
        resp.raise_for_status()
        result = resp.json()
        if not result.get('ok', False):
            print(f'HTTP file transfer failed: {result}')
            return False
        return True

    transfer_id = str(uuid4())
    url = f'http://{ip}:{port}/sendfilessocket/init/{app_name}'
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
            init_resp = requests.post(url, json=init_payload)
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

    sock_port = int(init_obj.get('file_socket_port', file_socket_port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, sock_port))
        s.settimeout(60)
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

    complete_url = f'http://{ip}:{port}/sendfilessocket/complete/{app_name}'
    complete_resp = requests.post(complete_url, json={'transfer_id': transfer_id})
    complete_resp.raise_for_status()
    complete_obj = complete_resp.json()
    if not complete_obj.get('ok', False):
        print(f"Transfer completion failed for {transfer_id}: {complete_obj.get('errors', [])}")
        return False
    return True



def receive_files(server_addr:tuple[str, int]) -> bool:
    ip, port = server_addr
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((ip, server_socket_port))









    


#Sort of like git push, but for uncommited changes and specifically from the server
def send_current_changes(server_addr:tuple[str, int]) -> bool:
    git_diff_path, changed_binary_paths = prepare_text_changes()
    paths = [os.path.join(get_runtime_dir_path(), 'gitdiff.diff'), *changed_binary_paths]
    print('sending paths:')
    print(paths)
    send_files(server_addr, paths)
    app_name = get_appname()
    ip, port = server_addr
    url = f'http://{ip}:{port}/sendchanges/{app_name}'
    resp:Response = requests.get(url)
    resp.raise_for_status()
    return True


def retrieve_changed_file_list_on_server(server_addr:tuple[str, int]) -> list[str]:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/retrieve_changed_file_paths/{app_name}'
    try:
        diff_resp:Response = requests.get(url, stream=True)
    except requests.RequestException as e:
        print(f'Failed to retrieve list of changed file paths: {e}')
        return []
    
    if not diff_resp.text:
        print('Received empty path list')
        return []

    changed_file_paths = diff_resp.text.split('\n')
    return changed_file_paths
    

def retrieve_current_text_changes(server_addr:tuple[str, int], save_as_filename='gitdiff.diff') -> bool:
    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/retrieve_text_changes/{app_name}'
    ran_successfully = True
    try:
        diff_resp:Response = requests.get(url, stream=True)
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

    #apply patch
    git_apply_command = f'git apply {git_patch_path}'
    os.system(git_apply_command)
    return ran_successfully


def retrieve_current_binary_changes(server_addr:tuple[str, int]) -> bool:
    ip, port = server_addr
    app_name = get_appname()
    project_root = get_project_root_path()
    url = f'http://{ip}:{port}/retrieve_changed_binary_paths/{app_name}'
    try:
        binary_paths_resp:Response = requests.get(url)
        binary_paths_resp.raise_for_status()
    except requests.RequestException as e:
        print(f'Failed to retrieve binary path list: {e}')
        return False
    if not binary_paths_resp.text:
        print('No changed binary files returned by server')

    #split binary file paths into a list
    paths = [path.strip() for path in binary_paths_resp.text.split('\n')]
    paths = [path for path in paths if path] #remove empty paths

    for path in paths:
        print(f'trying to save binary file to path: {path}')
        #verify that the directory in which we are supposed to write the binary file to already exists.  If not, create it and all intermediate directories with os.makedirs
        parent_dir = os.path.dirname(path)

        #if parent_dir is an empty string, this means that the "path" is actually just a filename.  So
        if parent_dir == '':
            parent_dir = get_project_root_path()

        if os.path.isabs(path):
            if is_subdir(path, project_root):
                path = os.path.realpath(path)
            else:
                print('ERROR: CANNOT ACCEPT FILES FROM OUTSIDE PROJECT ROOT')
                return False

        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        filename = os.path.split(path)[-1]
        print(f'Retrieving {filename} from server')
        sanitized_path = urllib.parse.quote(path, safe='/')
        url = f'http://{ip}:{port}/retrieve_binary_file/{app_name}/{sanitized_path}'
        try:
            resp:Response = requests.get(url, stream=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f'Failed to retrieve binary file {path}: {e}')
            ran_successfully = False
            continue
        chunk_size = 1024 * 8
        with open(path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
    
    return True #the idea is just to return True/False based on whether everything runs successfully or not, but I haven't really implemented that yet


#Sort of like git pull, but for uncommitted changes and specifically to the server
def retrieve_current_changes(server_addr:tuple[str, int], exclude_binary_changes=False, save_as_filename='gitdiff.diff') -> bool:
    retrieved_text_changes = retrieve_current_text_changes(server_addr, save_as_filename)
    if not exclude_binary_changes:
        project_root = get_project_root_path()
        ip, port = server_addr
        app_name = get_appname()
        url = f'http://{ip}:{port}/retrieve_changed_binary_paths/{app_name}'
        try:
            binary_paths_resp:Response = requests.get(url)
            binary_paths_resp.raise_for_status()
        except requests.RequestException as e:
            print(f'Failed to retrieve binary path list: {e}')
            return False
        if not binary_paths_resp.text:
            print('No changed binary files returned by server')

        #split binary file paths into a list
        paths = [path.strip() for path in binary_paths_resp.text.split('\n')]
        paths = [path for path in paths if path] #remove empty paths
        retrieved_binary_changes = receive_files_from_server(server_addr, paths)
        # retrieved_binary_changes = retrieve_current_binary_changes(server_addr)
        return retrieved_text_changes and retrieved_binary_changes
    return retrieved_text_changes


def get_current_server_commit_hash(server_addr:tuple[str, int], app_name:str='') -> str|None:
    ip, port = server_addr
    if app_name == '':
        app_name = get_appname()

    url = f'http://{ip}:{port}/retrieve_current_commit_hash/{app_name}'

    try:
        resp:Response = requests.get(url)
    except requests.RequestException as e:
        print(f"Failed to retrieve server's current commit hash")
        return None
    
    if not resp.text:
        print(f"Received empty string as server's current commit hash")
        return None

    commit_hash = resp.text.strip()
    return commit_hash


def retrieve_git_branches_from_server(server_addr:tuple[str, int], app_name:str='', sort_order:str='creatordate') -> list[str] | tuple[list[str], str]:
    ip, port = server_addr
    valid_sort_orders = ['creatordate', 'committerdate', 'taggerdate', 'authordate']
    if not sort_order in valid_sort_orders:
        sort_order = valid_sort_orders[0]
    url = f'http://{ip}:{port}/retrieve_git_branches/{app_name}/{sort_order}'
    try:
        resp = requests.get(url)
    except requests.RequestException as e:
        print("Failed to retrieve server's local git branches")
        return []
    
    if not resp.text:
        print(f"Received an empty string as server's git branches.  Something almost certainly went wrong")
        return []

    resp_obj = json.loads(resp.text)
    server_branches = resp_obj['branches']
    server_current_branch = resp_obj['current_branch']
    return server_branches, server_current_branch


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
    ip, port = server_addr
    if not app_name:
        app_name = get_appname()

    url = f'http://{ip}:{port}/git_state/{app_name}'
    try:
        resp:Response = requests.get(url)
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
    ip, port = server_addr
    if not app_name:
        app_name = get_appname()
    if args is None:
        args = {}

    url = f'http://{ip}:{port}/git_action/{app_name}'
    payload = {'action': action, 'args': args}
    try:
        resp:Response = requests.post(url, json=payload)
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
            message='Tracked uncommitted changes detected; reconcile requires a clean tracked worktree on both sides.',
        )

    local_has_server_head = git_has_commit(server_head, cwd=get_project_root_path())
    server_has_local_result = _post_server_git_action(
        server_addr,
        'has_commit',
        args={'commit': local_head},
        app_name=app_name,
    )
    if server_has_local_result is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            actions_applied=actions_applied,
            message='Failed while checking whether server can resolve client HEAD commit.',
        )
    server_has_local_head = bool(server_has_local_result.get('has_commit', False))

    if not local_has_server_head:
        if not local_state.get('has_origin', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_NO_ORIGIN,
                actions_applied=actions_applied,
                message='Client is missing server commit and has no origin remote for fetch.',
            )
        fetch_local = _run_local_git_action('fetch_origin', {})
        actions_applied.append('client:fetch_origin')
        if not fetch_local.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_MISSING_COMMIT_OBJECT,
                actions_applied=actions_applied,
                message='Client fetch from origin failed while trying to obtain server commit.',
            )

    if not server_has_local_head:
        if not server_state.get('has_origin', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_NO_ORIGIN,
                actions_applied=actions_applied,
                message='Server is missing client commit and has no origin remote for fetch.',
            )
        fetch_server = _post_server_git_action(server_addr, 'fetch_origin', args={}, app_name=app_name)
        actions_applied.append('server:fetch_origin')
        if not fetch_server or not fetch_server.get('success', False):
            return _reconcile_result(
                RECONCILE_STATUS_BLOCKED_MISSING_COMMIT_OBJECT,
                actions_applied=actions_applied,
                message='Server fetch from origin failed while trying to obtain client commit.',
            )

    # Re-check commit visibility after fetches.
    local_has_server_head = git_has_commit(server_head, cwd=get_project_root_path())
    server_has_local_result = _post_server_git_action(
        server_addr,
        'has_commit',
        args={'commit': local_head},
        app_name=app_name,
    )
    if server_has_local_result is None:
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            actions_applied=actions_applied,
            message='Failed while re-checking server commit visibility after fetch.',
        )
    server_has_local_head = bool(server_has_local_result.get('has_commit', False))

    if not local_has_server_head or not server_has_local_head:
        missing_bits = []
        if not local_has_server_head:
            missing_bits.append('client_missing_server_commit')
        if not server_has_local_head:
            missing_bits.append('server_missing_client_commit')
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
    ip, port = server_addr
    app_name:str = get_appname()
    url = f'http://{ip}:{port}/retrieve_diff_for_files/{app_name}'
    try:
        resp:Response = requests.post(url, json={'filepaths': paths})
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
    ip, port = server_addr
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
    url = f'http://{ip}:{port}/retrieve_changed_file_paths/{app_name}/{scope}'
    changed_fpaths_client = get_changed_file_paths(scope)
    changed_plainpaths_client, changed_binarypaths_client = split_paths_by_text_or_binary(changed_fpaths_client)

    try:
        resp = requests.get(url, stream=True)
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
        client_diff_path = get_diff_for_files(client_only_plaintext_paths, 'client_plaintext_diff.diff')
        url = f'http://{ip}:{port}/'
        success = send_files(server_addr, [client_diff_path])
        if not success:
            print(f'Failed to send client plaintext files patch to server')
            return False
        
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

    cwd = unix_path(os.getcwd())
    server_ip, server_port = '192.168.7.189', get_server_port() 
    server_addr = (server_ip, server_port)
    BUILD_SUCCESS = '** BUILD SUCCEEDED **'
    BUILD_FAILED = '** BUILD FAILED **'



    runtime_dir = get_runtime_dir_name()
    diffs_path = unix_path(os.path.join(cwd, runtime_dir))
    gitignore_path = os.path.join(cwd, '.gitignore')
    git_diff_filepath = unix_path(os.path.join(diffs_path, 'gitdiff.diff'))
    git_add_command = 'git add .'



    update_gitignore()


    #if 
    if os.path.exists(diffs_path):
        if not os.path.isdir(diffs_path): #diffs_path exists but is a file instead of a directory.  Delete it, and a make a directory in its place
            os.remove(diffs_path)
            os.mkdir(diffs_path)
    else:
        os.mkdir(diffs_path)
    #End of all first-run initialization




    if len(sys.argv) > 1:
        arg = sys.argv[1]
    else:
        arg = 'build'


    if 'build' in arg: #for now, just to allow for flexibility in testing
        git_diff_filepath, changed_binary_paths = prepare_text_changes()
        resp:Response = start_build_job(server_addr, git_diff_filepath, changed_binary_paths)
        json_obj = json.loads(resp.text)
        job_id = json_obj['job_id']
        build_log_str = wait_for_build_completion(server_addr, job_id)
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
    
