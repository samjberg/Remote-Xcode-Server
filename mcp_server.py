import os, subprocess, socket
from flask import Flask, request, jsonify, Request, Response
from threading import Thread
from werkzeug.utils import secure_filename
from uuid import uuid4
from mcp_utils import *
from typing import BinaryIO
# from requests import Request



cwd = unix_path(os.getcwd())
project_name = get_project_name()
server_dir_name = 'uploads'
server_dir_path = os.path.join(cwd, server_dir_name)
project_info_filename = 'projectinfo.txt'
project_info_filepath = os.path.join(server_dir_path, project_info_filename)

server_socket_port = 50271 
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(('0.0.0.0', server_socket_port))
server.listen(1)

if '.gitignore' not in os.listdir():
    with open('.gitignore', 'w') as f:
        server_dir_ignore_line = f'/{server_dir_name}/'
        f.writelines([server_dir_ignore_line])
else:
    ignores_uploads = False
    ignores_diffs = False
    with open('.gitignore', 'r') as f:
        for line in f.readlines():
            for s in ['/uploads/', 'uploads/']:
                if s==line or s in line:
                    ignores_uploads = True
            for s in ['/diffs/', 'diffs/']:
                if s == line or s in line:
                    ignores_diffs = True
            if ignores_uploads and ignores_diffs:
                break
    
    if not (ignores_uploads and ignores_diffs):
        with open('.gitignore', 'a') as f:
            if not ignores_uploads:
                f.write('/uploads/\n')
            if not ignores_diffs:
                f.write('/diffs/\n')



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






UPLOAD_FOLDER = unix_path(os.path.join(cwd, 'uploads'))
git_diff_filepath = unix_path(os.path.join(UPLOAD_FOLDER, 'gitdiff.diff'))

JOBS = {}
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
build_log_path = os.path.join(UPLOAD_FOLDER, 'build-fallbackid.txt')








def run_xcodebuild(job_id):
    conn, addr = server.accept()
    print(f'Received connection from {addr}')
    try:
        job = JOBS[job_id]
        job['status'] = 'running'
        xcodebuild_command: str = f"xcodebuild -scheme \"{project_name}\" -destination 'generic/platform=iOS Simulator' build"
        proc = subprocess.Popen(xcodebuild_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, shell=True)
        chunk_size = 4096
        build_log_name = f'buildlog-{job_id}.txt'
        build_log_path = os.path.join(UPLOAD_FOLDER, build_log_name)
        log_write = open(build_log_path, 'w')
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            conn.sendall(chunk)

        log_write.close()
        proc.wait()
        conn.close()

    except Exception as e:
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)











@app.route('/appname/<appname>', methods=['GET', 'POST'])
def start_build_job(appname):
    print(f'appname: {appname}')

    if request.method == 'POST' or request.method == 'GET':
        if 'file' not in request.files:
            print('NO FILE PART')
            return 'NO FILE PART'

        file = request.files['file']
        print(f'file: {file}')
        print(f'file.filename: {file.filename}')
        print(f'file.name: {file.name}')
        if file == '':
            print('empty filename')
            return 'empty filename'

        print(f'request.files: {request.files}')


        if file and allowed_filename(file.filename):
            #create a secure version of the filename
            filename = secure_filename(file.filename)
            #save the file with the secure filename in UPLOAD_FOLDER
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

            patch_path = f'{app.config["UPLOAD_FOLDER"]}/{filename}'
            git_apply_command = f'git apply {patch_path}'
            #run the git apply command
            os.system(git_apply_command)


            job_id = str(uuid4())
            if job_id in JOBS.keys():
                return f'<p>Already building {appname}, job_id: {job_id}</p>'

            build_log_name:str = f'buildlog_{job_id}.txt'
            build_log_path:str = os.path.join(UPLOAD_FOLDER, build_log_name)

            #Create the new job object and put it in job_id in the JOBS dict.
            #We have to be careful to ensure the file gets closed
            build_log_file = open(build_log_path, 'w')
            JOBS[job_id] = {"status": "pending", "result": '', "error": None, "file_descriptor": build_log_file}

            t = Thread(target=run_xcodebuild, args=([job_id]), daemon=True)
            t.start()
            return jsonify({"job_id": job_id}), 202
        else:
            return 'No file, or disallowed file type was uploaded'
    else:
        return "Some other method besides POST or GET was used.  Don't do that"
        


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
    with open(git_diff_filepath, 'r') as f:
        text = f.read()
    return text



if __name__ == '__main__':
    ip, port = '0.0.0.0', get_server_port()
    app.run(ip, port)








