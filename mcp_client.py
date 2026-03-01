import sys, os, socket, requests, json, urllib
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

#Sort of like git push, but for uncommited changes and specifically from the server
def send_current_changes(server_addr:tuple[str, int]) -> Response:
    git_diff_path, changed_binary_paths = prepare_text_changes()

    ip, port = server_addr
    app_name = get_appname()
    url = f'http://{ip}:{port}/sendchanges/{app_name}'
    filename = 'gitdiff.diff'

    files = {'gitdiff': (filename, open(git_diff_path, 'rb'), 'text/plain', {'Expires': 0})}

    
    for i, path in enumerate(changed_binary_paths):
        path = unix_path(path)
        print(f'Adding {path} to POST request')
        mimetype, encoding = guess_type(path, strict=False)
        if not mimetype:
            mimetype = 'application/octet-stream'
        files[f'binaryfile{i}'] = (path, open(path, 'rb'), mimetype, {'Expires': 0})
    print(f'Sending changes located at {git_diff_path} to {url}\n')
    resp = requests.post(url, files=files)
    return resp



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
    



#Sort of like git pull, but for uncommitted changes and specifically to the server
def retrieve_current_changes(server_addr:tuple[str, int], save_changes=True, save_as_filename='gitdiff.diff') -> bool:
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

    #request paths for changed binary files
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
        #verify that the directory in which we are supposed to write the binary file to already exists.  If not, create it and all intermediate directories with os.makedirs
        parent_dir = os.path.dirname(path)
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
    
    return ran_successfully #the idea is just to return True/False based on whether everything runs successfully or not, but I haven't really implemented that yet

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
        print(f"Recieved an empty string as server's git branches.  Something almost certainly went wrong")
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
        return _reconcile_result(
            RECONCILE_STATUS_ERROR,
            authority_side=authority_side,
            target_branch=target_branch,
            target_commit=target_commit,
            actions_applied=actions_applied,
            message=f'Failed action {non_authoritative_side}:{first_action}',
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
            return _reconcile_result(
                RECONCILE_STATUS_ERROR,
                authority_side=authority_side,
                target_branch=target_branch,
                target_commit=target_commit,
                actions_applied=actions_applied,
                message=f'Failed action {non_authoritative_side}:ff_only_to_commit',
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


def sync_changes_with_server(server_addr:tuple[str, int], sync_branches=False, scope='repo') -> bool:
    ip, port = server_addr
    app_name = get_appname()
    reconcile_result = reconcile_git_state(server_addr, app_name=app_name)
    reconcile_status = reconcile_result.get('status', RECONCILE_STATUS_ERROR)
    if reconcile_status not in [RECONCILE_STATUS_ALIGNED, RECONCILE_STATUS_RECONCILED]:
        print(f'Phase 1 reconcile blocked: {reconcile_status}')
        print(f"Reason: {reconcile_result.get('message', '')}")
        if reconcile_result.get('actions_applied'):
            print(f"Actions applied before stop: {', '.join(reconcile_result['actions_applied'])}")
        return False


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
    else:
        print(f'Invalid argument: {arg}')
    
