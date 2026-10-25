"""Isolated GUI sender; no Tk, no API token, one invocation per ledger job."""
from __future__ import annotations
import contextlib
from dataclasses import asdict
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import threading

ROOT=Path(__file__).resolve().parent
from .app_paths import PYTHON, RUNTIME
from .simple_core import ForwardError, require, safe_code, SAFE_RETRY_REASONS


def attempt_paths(job):
    identifier=job.get('job_id',job.get('id'))
    require(type(identifier) is int and identifier>0,'INVALID_JOB_ID')
    ticket=job.get('attempt_id')
    require(ticket is None or isinstance(ticket,str) and re.fullmatch(r'[a-f0-9]{32}',ticket),'INVALID_ATTEMPT_ID')
    stem=str(identifier) if ticket is None else str(identifier)+'-'+ticket
    return RUNTIME/'dispatch'/(stem+'.json'),RUNTIME/'dispatch'/(stem+'.cancel')


def verify_safe_retry(job):
    """Read a completed pre-submit receipt; never clear or rewrite its lock."""
    if job.get('state')!='BLOCKED' or job.get('reason') not in SAFE_RETRY_REASONS:return False
    try:
        receipt,_=attempt_paths(job)
        if job['reason']=='STOPPED_BEFORE_CHILD' and not receipt.exists():return True
        old=json.loads(receipt.read_text(encoding='utf-8'))
        result=old.get('result',{})
        return (old.get('job_id')==job['id'] and old.get('body_sha256')==job['content_hash']
                and old.get('phase')=='FINISHED' and result.get('status')=='BLOCKED'
                and result.get('reason')==job['reason']
                and old.get('submission_possible',False) is False)
    except (OSError,ValueError,TypeError):return False


_process_lock=threading.Lock()
_active_processes=set()


def executor_idle():
    with _process_lock:
        finished=[p for p in _active_processes if p.poll() is not None]
        for p in finished:_active_processes.discard(p)
        return not _active_processes


@contextlib.contextmanager
def tracked_process(*args,**kwargs):
    process=None
    try:
        with subprocess.Popen(*args,**kwargs) as process:
            with _process_lock:_active_processes.add(process)
            yield process
    finally:
        # Popen.__exit__ has waited; never infer idleness from a status string.
        if process is not None and process.poll() is not None:
            with _process_lock:_active_processes.discard(process)


def process_command():
    return [str(PYTHON),'-I','-B','-m','wechat_desktop_sender.sender_worker','--child']


def call_child(job,route,stop):
    directory=RUNTIME/'dispatch'
    directory.mkdir(parents=True,exist_ok=True)
    receipt,cancel=attempt_paths(job)
    # A previously started attempt is never repeated, regardless of its receipt.
    if receipt.exists():
        return {'status':'UNKNOWN','reason':'PRIOR_CHILD_ATTEMPT_NO_RETRY'}
    if stop.is_set():return {'status':'BLOCKED','reason':'STOPPED_BEFORE_CHILD'}
    request={'job_id':job['id'],'attempt_id':job.get('attempt_id'),'text':job['text'],'route':asdict(route)}
    payload=json.dumps(request,ensure_ascii=True).encode('utf-8')
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    with tracked_process(process_command(),cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,creationflags=flags) as process:
        process.stdin.write(payload)
        process.stdin.close()
        deadline=time.monotonic()+120
        while process.poll() is None:
            if stop.is_set():cancel.touch(exist_ok=True)
            if time.monotonic()>=deadline:
                process.terminate()
                process.wait(timeout=10)
                return {'status':'UNKNOWN','reason':'CHILD_TIMEOUT_NO_RETRY'}
            time.sleep(.1)
        output=process.stdout.read(32769)
        try:
            require(len(output)<=32768,'CHILD_OUTPUT_INVALID')
            result=json.loads(output.decode('utf-8'))
            require(isinstance(result,dict),'CHILD_OUTPUT_INVALID')
            return result
        except Exception:
            return {'status':'UNKNOWN','reason':'CHILD_RESULT_MISSING_NO_RETRY'}


def submit(request, *, search_run=None, paste_backend=None, directory_reader=None):
    from .live_capture_once import Stop,save
    from .configuration import read_route as read_route_directory
    route=request['route']
    current=(directory_reader or read_route_directory)()
    for key in ('account_id','account_root','source_id','target_id','source_alias','target_alias'):
        require(getattr(current,key)==route[key],'ROUTE_METADATA_CHANGED')
    require(type(request['job_id']) is int and request['job_id']>0,'INVALID_JOB_ID')
    body=request['text']
    require(isinstance(body,str) and 0<len(body)<=10000 and '\x00' not in body,'UNSUPPORTED_TEXT')
    directory=RUNTIME/'dispatch'
    directory.mkdir(parents=True,exist_ok=True)
    path,cancel=attempt_paths(request)
    record={'job_id':request['job_id'],'body_sha256':sha256(body.encode('utf-8')).hexdigest(),
            'phase':'PREPARING','retry_allowed':False}
    with path.open('x',encoding='utf-8') as f:
        json.dump(record,f);f.flush();os.fsync(f.fileno())
    final={}
    report_box={}
    def cancelled():
        if cancel.exists():raise Stop('STOP_USER_CANCELLED')
    def opened(context):
        cancelled()
        report=context['report']
        require(report.get('opened_remark_verified')==current.target_alias,'OPENED_REMARK_MISMATCH')
        context['stable']()
        context['port'].require_activity(context['activity_baseline'])
        if paste_backend is None:
            import pyautogui,pyperclip
            pyautogui.FAILSAFE=True
            copy,hotkey,press=pyperclip.copy,pyautogui.hotkey,pyautogui.press
        else:copy,hotkey,press=paste_backend()
        cancelled()
        record['phase']='PASTE_PENDING';save(path,record)
        copy(body)  # Exact raw text. No prefix, renumbering, trimming or splitting.
        cancelled();context['stable']()
        report.update(paste_calls=1,chat_editor_input_calls=1,editor_focus_probes=0,editor_focus_clicks=0)
        hotkey('ctrl','v',logScreenshot=False)
        record['phase']='PASTE_RETURNED';save(path,record)
        time.sleep(.15)
        cancelled();context['stable']()
        record['phase']='SUBMIT_PENDING';save(path,record)
        report.update(enter_calls=1,sends=1)
        press('enter',presses=1,interval=0,logScreenshot=False)
        record['phase']='SUBMIT_RETURNED';save(path,record)
        final.update(status='SUBMITTED_UNVERIFIED',reason='ENTER_RETURNED_NO_SERVER_ACK')
        # No editor-focus probe, extra click, draft readback or second Enter.
        return {'status':'SUBMIT_REQUESTED_UNVERIFIED','retry_allowed':False,'server_acknowledgement':False}
    try:
        cancelled()
        if search_run is None:
            from . import search_once
            search_run=search_once.run
        search_run(None,search_alias=current.target_alias,inspect_open_once=True,
                   expected_open_remark=current.target_alias,on_open=opened,
                   taskbar_delay_seconds=0,allow_existing_foreground=True,
                   result_sink=lambda report,folder:report_box.update(report=report))
        if not final:
            report=report_box.get('report',{})
            code=report.get('status','SEARCH_DID_NOT_OPEN_TARGET')
            final={'status':'UNKNOWN' if record['phase']!='PREPARING' else 'BLOCKED',
                   'reason':code if isinstance(code,str) and code.startswith('STOP_') else 'SEARCH_DID_NOT_OPEN_TARGET'}
    except BaseException as error:
        reason=str(error) if isinstance(error,Stop) and re.fullmatch(r'STOP_[A-Z0-9_]{1,90}',str(error)) else safe_code(error)
        final={'status':'UNKNOWN' if record['phase']!='PREPARING' else 'BLOCKED',
               'reason':reason}
    record.update(result=final,submission_possible=record['phase']!='PREPARING',phase='FINISHED')
    save(path,record)
    return final


if __name__=='__main__':
    result={'status':'BLOCKED','reason':'EXPLICIT_CHILD_INPUT_REQUIRED'}
    if sys.argv[1:]==['--child']:
        try:
            data=sys.stdin.buffer.read(256*1024+1)
            require(len(data)<=256*1024,'CHILD_INPUT_TOO_LARGE')
            request=json.loads(data.decode('utf-8'))
            from .dpi_bootstrap import initialize_process_dpi
            initialize_process_dpi()
            # Legacy diagnostic prints are discarded; no token is passed to this process.
            with open(os.devnull,'w',encoding='utf-8') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                result=submit(request)
        except BaseException as error:result={'status':'UNKNOWN','reason':safe_code(error)}
    print(json.dumps(result,ensure_ascii=True),flush=True)
