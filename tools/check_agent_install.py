"""Verify installed/frozen Agent interfaces outside the source checkout."""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = r'''
import asyncio,base64,importlib.metadata,json,os,struct,subprocess,sys,time,zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pagewriter import __version__
from pagewriter.interfaces.client import ApiClient
from mcp import Client, StdioServerParameters
workspace=Path.cwd()
command=[sys.argv[1]] if sys.argv[1] else [sys.executable,'-I','-m','pagewriter.interfaces.cli']
connection=workspace/'connection.json'
assert importlib.metadata.version('maliang')==__version__
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
process=subprocess.Popen(command+['serve','--workspace',str(workspace),'--connection',str(connection)],
    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',creationflags=flags)
def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
def call(method,params=None):
    response=client.call(method,params or {})
    assert response.get('ok'),(method,response)
    return response['result']
try:
    with ThreadPoolExecutor(max_workers=1) as pool:
        ready=json.loads(pool.submit(process.stdout.readline).result(timeout=40))
    assert ready['ok'] and ready['result']['ready'] and 'token' not in ready['result']
    client=ApiClient(connection,timeout=35)
    info=call('system.info');assert info['version']==__version__
    assert not call('device.status')['connection']
    assert len(call('fonts.list')['fonts'])==9
    photo=workspace/'reference.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',8,8,8,2,0,0,0))+
        chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\xff\xff'*8)*8))+chunk(b'IEND',b''))
    revision=call('document.get')['revision']
    edited=call('document.apply',{'expected_revision':revision,'operations':[
        {'op':'import_photo','path':'reference.png','quad':[[0,0],[8,0],[8,8],[0,8]]},
        {'op':'add_text','id':'acceptance','x_mm':20,'y_mm':20,'width_mm':80,
         'runs':[{'text':'中 A','font_id':'maoken','size_pt':14,'underline':True}],
         'columns':2,'column_height_mm':15,'column_gap_mm':6,'column_separators':True},
        {'op':'add_zone','id':'printed','kind':'ink','points_mm':[[130,20],[150,20],[150,40],[130,40]]} ]})
    preview=call('preview.render');assert Path(preview['path']).read_bytes().startswith(b'\x89PNG')
    assert preview['preflight']['passed'] and len(preview['pixel_to_page_mm'])==6
    call('document.export',{'path':'annotation.svg','format':'svg'})
    call('document.save',{'path':'annotation.pagewriter'})
    expected=call('document.get')['blocks'][0]['text'];assert expected=='中 A'
    call('document.new');call('document.open',{'path':'annotation.pagewriter'})
    assert call('document.get')['blocks'][0]['text']==expected
    call('device.connect',{'profile_id':'simulator_servo'})
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        state=call('device.status')
        if state.get('authorized'):break
        time.sleep(.05)
    assert state['capabilities']['axes']==['X','Y'] and state['simulation']
    plan=call('job.prepare');assert plan['runnable'],plan
    job=call('job.start',{'plan_id':plan['plan_id'],'request_id':'installed-acceptance'})
    assert call('job.start',{'plan_id':plan['plan_id'],'request_id':'installed-acceptance'})['job_id']==job['job_id']
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        result=call('job.status',{'job_id':job['job_id']})
        if result['state']=='complete':break
        assert result['state'] not in ['failed','rejected','unknown'],result
        time.sleep(.05)
    assert result['state']=='complete' and result['completion_evidence']=='virtual_observation',result
    async def mcp_probe():
        params=StdioServerParameters(command=command[0],args=command[1:]+['mcp','--connection',str(connection)],cwd=workspace)
        async with Client(params,read_timeout_seconds=20) as mcp:
            tools=await mcp.list_tools();assert any(tool.name=='document.apply' for tool in tools.tools)
            result=await mcp.call_tool('document.get',{})
            assert not result.is_error and result.structured_content['result']['blocks'][0]['text']==expected
            rendered=await mcp.call_tool('preview.render',{})
            assert not rendered.is_error and any(content.type=='image' for content in rendered.content)
    asyncio.run(mcp_probe())
    print('Installed Agent: shared document/PNG/SVG/save-reopen, no-Z virtual job/idempotency and real stdio MCP verified.')
finally:
    if process.poll() is None:process.terminate()
    stdout,stderr=process.communicate(timeout=15)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent-exe', type=Path, help='Frozen MaliangAgent.exe to verify instead of installed CLI')
    args = parser.parse_args()
    command = str(args.agent_exe.resolve()) if args.agent_exe else ''
    with tempfile.TemporaryDirectory(prefix='maliang-agent-install-') as folder:
        subprocess.run([sys.executable, '-I', '-c', PROBE, command], cwd=folder, check=True, timeout=120)


if __name__ == '__main__':
    main()
