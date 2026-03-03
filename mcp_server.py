import os, subprocess, socket, json, struct, hashlib, time
from flask import Flask, request, send_file, send_from_directory, jsonify, Request, Response
from threading import Thread, Lock
from werkzeug.utils import secure_filename
from mcp_utils import *
# from requests import Request



cwd = unix_path(os.getcwd())
project_name = get_project_name()
server_dir_name = get_runtime_dir_name()
server_dir_path = os.path.join(cwd, server_dir_name)
project_info_filename = 'projectinfo.txt'
project_info_filepath = os.path.join(server_dir_path, project_info_filename)

server_socket_port = 50271 
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('0.0.0.0', server_socket_port))
server.listen(1)
filesocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
filesocket.bind(('0.0.0.0', file_socket_port))
filesocket.listen(1)

update_gitignore()



if not uploads_folder_exists():
    os.mkdir(server_dir_path)

if project_info_filename not in os.listdir(server_dir_path): #this means this is the first time the server is being run, 
        with open(project_info_filepath, 'w') as f:
            config_lines = [f'project_root:{cwd}\n', f'project_name:{project_name}\n']
            for line in config_lines:
                if line[-1] in '\n\r':
                    f.write(line)
                else:
                    f.write(line + '\n')
            # f.writelines(config_lines)






UPLOAD_FOLDER = unix_path(os.path.join(cwd, server_dir_name))
JOBS = {}
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
TRANSFER_SESSIONS: dict[str, dict] = {}
OUTBOUND_TRANSFER_SESSIONS: dict[str, dict] = {}
SESSION_LOCK = Lock()
SESSION_TTL_SECONDS = 30 * 60




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

    project_root_abs = os.path.abspath(cwd)
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
        conn, _ = filesocket.accept()
        conn.settimeout(60)
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

        conn, _ = filesocket.accept()
        conn.settimeout(60)
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



def run_xcodebuild(job_id):
    conn, addr = server.accept()
    print(f'Received connection from {addr}')
    try:
        job = JOBS[job_id]
        job['status'] = 'running'
        xcodebuild_command: str = f"xcodebuild -scheme \"{project_name}\" -destination 'generic/platform=iOS Simulator' build"
        proc = subprocess.Popen(xcodebuild_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, shell=True)
        chunk_size = 4096
        log_write = job['file']
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            log_write.write(chunk.decode())
            conn.sendall(chunk)

        log_write.close()
        proc.wait()
        conn.close()

    except Exception as e:
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)


def _send_file_over_socket(path:str, chunk_size=32*KB) -> bool:
    print('Legacy _send_file_over_socket is disabled. Use /sendfilessocket/init and /complete flow.')
    return False



def _receive_file_over_socket(conn:socket.socket=server) -> bool:
    print('Legacy _receive_file_over_socket is disabled. Use framed transfer session handlers.')
    return True





@app.route('/retrieve_text_changes/<appname>')
def send_changes(appname:str):
    git_diff_path, changed_binary_paths = prepare_text_changes()
    git_diff_dir, git_diff_name = [unix_path(p) for p in os.path.split(git_diff_path)]
    return send_from_directory(git_diff_dir, git_diff_name, as_attachment=True)

@app.route('/retrieve_changed_binary_paths/<appname>')
def send_changed_binary_paths(appname:str):
    changed_file_paths = [path for path in get_changed_file_paths() if path]
    changed_binary_paths = [path for path in changed_file_paths if not is_plaintext(path.split('/')[-1])]
    paths_str = '\n'.join(changed_binary_paths)
    return paths_str

@app.route('/retrieve_diff_for_files/<appname>', methods=['POST'])
def send_diff_for_files(appname:str):
    try:
        paths = request.json['filepaths']
    except KeyError as e:
        print(f'Error: key filepaths not found in request json.  err: {e}')

    git_diff_path = get_diff_for_files(paths)
    diffs_path, git_diff_name = os.path.split(git_diff_path)
    return send_from_directory(diffs_path, git_diff_name)


@app.route('/retrieve_current_commit_hash/<appname>')
def send_current_commit_hash(appname:str):
    current_commit_hash = get_current_commit_hash()
    return current_commit_hash


@app.route('/retrieve_git_branches/<appname>/<sort_order>')
def send_git_branches(appname:str, sort_order:str):
    git_branches, current_branch = get_git_branches(appname, return_current_branch=True, sort_order=sort_order)
    return jsonify({'branches': git_branches, 'current_branch': current_branch})


@app.route('/git_state/<appname>')
def send_git_state(appname:str):
    try:
        state = get_git_state(get_project_root_path())
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify(state)


@app.route('/git_action/<appname>', methods=['POST'])
def run_git_action(appname:str):
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

    result = execute_git_action(action, action_args, cwd=get_project_root_path())
    if 'command' not in result and not result.get('success', False):
        return jsonify(result), 400
    return jsonify(result)



#note that <path:path> is NOT representing <variablename:variablename>. The full syntax for route variables is <converter:name>
#so it is just a coincidence that the thing I'm trying to pass here as a variable in the url IS literally a path, which happens to be
#the same as the name of the converter we need to use: "path".  The path converter makes it so that we can pass nested paths here, instead
#of paths getting cut off once they reach the first '/' (the first path delimiter)
@app.route('/retrieve_binary_file/<appname>/<path:path>')
def send_binary_file(appname:str, path:str):
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


@app.route('/sendchanges/<appname>', methods=['POST'])
def receive_changes(appname):
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



@app.route('/sendfileshttp/<appname>', methods=['POST'])
def receive_files_http(appname:str):
    saved_files = []
    errors = []
    for key in request.files.keys():
        file = request.files[key]
        if not file:
            errors.append(f'No file object for request.files key: {key}')
            continue
        rel_path = unix_path(file.filename)
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


@app.route('/sendfilessocket/init/<appname>', methods=['POST'])
def init_receive_files_socket(appname:str):
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


@app.route('/sendfilesfromserver/init/<appname>', methods=['POST'])
def init_send_files_from_server(appname: str):
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


@app.route('/sendfilesfromserver/complete/<appname>', methods=['POST'])
def complete_send_files_from_server(appname: str):
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


@app.route('/sendfilessocket/complete/<appname>', methods=['POST'])
def complete_receive_files_socket(appname:str):
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


@app.route('/sendfilessocket/<appname>', methods=['POST'])
def receive_files_socket_legacy(appname:str):
    try:
        data = request.get_json(silent=True) or {}
        paths = data.get('filepaths', [])
    except Exception:
        paths = []
    return jsonify(
        {
            'ok': False,
            'errors': ['Legacy route no longer supported. Use /sendfilessocket/init/<appname> and /sendfilessocket/complete/<appname>.'],
            'paths_seen': paths,
        }
    ), 410


@app.route('/appname/<appname>', methods=['GET', 'POST'])
def start_build_job(appname):
    print(f'appname: {appname}')

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
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

            patch_path = f'{app.config["UPLOAD_FOLDER"]}/{filename}'
            if os.path.getsize(patch_path) > 0:
                git_apply_command = f'git apply {patch_path}'
                #run the git apply command
                os.system(git_apply_command)


            job_id = str(uuid4())
            if job_id in JOBS.keys():
                return f'<p>Already building {appname}, job_id: {job_id}</p>'

            build_log_name:str = f'buildlog-{job_id}.txt'
            build_log_path:str = os.path.join(UPLOAD_FOLDER, build_log_name)

            #Create the new job object and put it in job_id in the JOBS dict.
            #We have to be careful to ensure the file gets closed
            build_log_file = open(build_log_path, 'w')
            JOBS[job_id] = {"status": "pending", "result": '', "error": None, "file": build_log_file}

            t = Thread(target=run_xcodebuild, args=([job_id]), daemon=True)
            t.start()
            return jsonify({"job_id": job_id}), 202
        else:
            return 'No file, or disallowed file type was uploaded'
    else:
        return "Some other method besides POST or GET was used.  Don't do that"
        

@app.route('/retrieve_changed_file_paths/<appname>/<scope>')
def send_changed_file_paths(appname:str, scope:str) -> Response:
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
    app.run(ip, port)








