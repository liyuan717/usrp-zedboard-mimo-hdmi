'use strict';
let csrf='',current={},lastImage='';
const $=id=>document.getElementById(id);
const names={idle:'准备就绪',preparing:'准备设备',transmitting:'有限发射',receiving:'采集新数据',processing:'接收处理',displaying:'HDMI 显示',verifying:'核验结果',complete:'本轮技术通过',stopping:'正在受控停止',stopped:'本轮已停止',failed:'本轮失败'};
function render(s){current=s;$('stage').textContent=names[s.stage]||s.stage;$('detail').textContent=s.detail||'';$('start').disabled=!!s.active;$('stop').disabled=!s.active||s.stage==='stopping';$('live').textContent=s.active?'运行中':'空闲';$('run').textContent=s.run_id||'—';$('directory').textContent=s.directory||'—';
  const error=s.stage==='stopped'?'':(s.result?.error||'');$('error').hidden=!error;$('error').textContent=error;$('setup').hidden=!error.includes('首次配置');
  if(s.hardware_action==='PS_RESET'){$('connection').textContent=s.detail;$('connection').classList.add('setup');}else{$('connection').textContent='保持 USRP、板卡上电，连接原网线、USB 串口和 HDMI。每轮会恢复已验证 RAM 环境。';$('connection').classList.remove('setup');}
  const steps=['preparing','transmitting','processing','displaying','complete'];const at=steps.indexOf(s.stage==='verifying'?'displaying':s.stage);document.querySelectorAll('.steps li').forEach((el,i)=>{el.classList.toggle('current',i===at);el.classList.toggle('done',i<at);});
  const r=s.result||{};$('adc').textContent=r.ADC_blocks?`${r.ADC_blocks} 块`:'—';$('rf').textContent=r.RF_frames??'—';$('crc').textContent=r.CRC_correct_packets??'—';$('hdmi').textContent=r.HDMI_frames?`${r.HDMI_frames} 帧`:'—';$('hash').textContent=r.ADC_sha256||'—';$('content').textContent=r.reconstructed_photo_pixels_match_TX?'接收编码及解码像素与发送端一致':'尚未核验';$('closed').textContent=r.LO_closed&&r.ADC_closed?'LO、ADC 已关闭':r.cleanup?.board_cleanup_confirmed?'已安全关闭':'尚未核验';$('result-status').textContent=r.technical_pass?'新空口 · 技术通过':s.active?'本轮进行中':s.stage==='stopped'?'已停止 · 日志已保存':r.error?'结果未通过':'等待运行';
  const show=!!r.reconstructed_photo_pixels_match_TX;$('photo').hidden=!show;$('empty').hidden=show;if(show&&lastImage!==s.run_id){lastImage=s.run_id;$('photo').src='/api/photo/'+s.run_id;}if(!show)lastImage='';$('visual').hidden=!r.technical_pass||s.active;
  const choices={stable:'已确认：稳定',accepted_minor_flicker:'已确认：轻微闪动，用户接受',unstable:'已记录：明显异常',not_seen:'已记录：未看到，本轮视觉未确认'};$('visual-result').textContent=choices[r.visual_confirmation?.choice]||'尚未提交视觉反馈';
}
async function request(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Photo-CSRF':csrf},body:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw Error(value.error||'操作失败');render(value.state);}
async function action(path,body={}){try{await request(path,body)}catch(error){$('error').hidden=false;$('error').textContent=error.message;}}
$('start').addEventListener('click',()=>action('/api/start'));
$('stop').addEventListener('click',()=>action('/api/stop'));
document.querySelectorAll('[data-choice]').forEach(button=>button.addEventListener('click',()=>action('/api/visual',{run_id:current.run_id,choice:button.dataset.choice})));
async function poll(){try{const response=await fetch('/api/state');if(!response.ok)throw Error('后台连接失败');const value=await response.json();csrf=value.csrf;render(value.state);}catch(error){$('detail').textContent='无法连接本机后台，请从桌面启动“MIMO 照片控制台”。';$('start').disabled=true;$('stop').disabled=true;}finally{setTimeout(poll,1000)}}poll();
