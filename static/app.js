'use strict';
const $=id=>document.getElementById(id);
let workspaceStarted=false;
let csrf='', attendees=[], meetingId=null, revision=0, savedStatus='draft', dirty=false, busy=false, selectedFile=null;
let page=1,totalPages=1,filterCategory='',selectedMeeting=null,aiConfigured=false,listSequence=0,transcript='',transcriptComplete=true;
let activeAnalysisId='', completedAnalysisId='', resumeAvailable=false;
const fields={title:'title',category:'category',meeting_date:'meeting-date',duration:'duration',author:'author',reporter:'reporter',source_text:'source-text',discussion:'discussion',notes:'notes'};
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function notice(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>$('toast').hidden=true,4200)}
function el(tag, text='', className=''){const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node}
function minutesText(tag,value){const node=el(tag);const text=String(value??'');const pattern=/\*\*(.+?)\*\*/gs;let offset=0;for(const match of text.matchAll(pattern)){node.append(document.createTextNode(text.slice(offset,match.index)),el('strong',match[1]));offset=match.index+match[0].length;}node.append(document.createTextNode(text.slice(offset)));return node;}
async function api(path, options={}){
 const headers={'X-CSRF-Token':csrf,...(options.headers||{})};
 if(options.body && !(options.body instanceof FormData) && !headers['Content-Type'])headers['Content-Type']='application/json';
 const response=await fetch(path,{credentials:'same-origin',...options,headers});
 const type=response.headers.get('content-type')||'';
 if(!response.ok){let data={};if(type.includes('json'))data=await response.json();if(response.status===401&&path!='/api/session'){showLogin();}const err=new Error(data.error||(response.status===413?'업로드 요청 용량을 초과했습니다. 녹음파일은 100 MB 이하인지 확인해 주세요.':`요청을 처리하지 못했습니다. (${response.status})`));err.code=data.code;err.status=response.status;err.data=data;throw err;}
 if(type.includes('json'))return response.json();return response;
}
async function uploadAudio(file){
 const setup=await api('/api/audio-uploads',{method:'POST',body:JSON.stringify({filename:file.name,size:file.size,mime:file.type})});
 const size=setup.chunk_size||8388608;
 for(let offset=0;offset<file.size;offset+=size){
  $('analysis-progress').querySelector('span:last-child').textContent=`녹음파일 업로드 중… ${Math.min(100,Math.round((offset/file.size)*100))}%`;
  const bytes=new Uint8Array(await file.slice(offset,Math.min(file.size,offset+size)).arrayBuffer());let binary='';
  for(let start=0;start<bytes.length;start+=32768)binary+=String.fromCharCode(...bytes.subarray(start,start+32768));
  const encoded=new URLSearchParams({offset:String(offset),chunk:btoa(binary)});
  await api('/api/audio-uploads/'+setup.upload_id+'/chunk',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:encoded.toString()});
 }
 return setup.upload_id;
}
function setAI(configured){aiConfigured=configured;$('ai-status').textContent=configured?'인증키 설정됨':'AI 연결 필요'}
function showLogin(){$('login-screen').hidden=false;$('workspace').hidden=true;}
async function initialize(){try{const s=await api('/api/session');csrf=s.csrf;if(s.authenticated)await showWorkspace(s);else showLogin();}catch(e){$('login-error').textContent='접속 상태를 확인할 수 없습니다. 새로고침해 주세요.'}}
async function showWorkspace(s){
 if(workspaceStarted){setAI(s.ai_configured);$('login-screen').hidden=true;$('workspace').hidden=false;return;}
 workspaceStarted=true;setAI(s.ai_configured);$('login-screen').hidden=true;$('workspace').hidden=false;
 $('today').textContent=new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',dateStyle:'long'}).format(new Date());
 const id=sessionStorage.getItem('medparkDraftId'),job=sessionStorage.getItem('medparkAnalysisJob');reset(false);
 if(id){try{loadEditor(await api('/api/meetings/'+id));sessionStorage.setItem('medparkDraftId',id);}catch(e){sessionStorage.removeItem('medparkDraftId');}}
 await route();loadAnalysisHistory();
 if(job){try{const status=await api('/api/analysis/'+job);if(!id)restoreAnalysisContext(status);sessionStorage.setItem('medparkAnalysisJob',job);await pollAnalysis(job,true);}catch(e){$('analysis-error').textContent=e.message;}}
}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{const fresh=await api('/api/session');csrf=fresh.csrf;const s=await api('/api/session',{method:'POST',body:JSON.stringify({password:$('password').value})});csrf=s.csrf;$('password').value='';await showWorkspace(s);}catch(e){$('login-error').textContent=e.message;}finally{button.disabled=false}});
$('logout').onclick=async()=>{if(dirty&&!confirm('저장하지 않은 내용이 있습니다. 로그아웃할까요?'))return;try{await api('/api/session',{method:'DELETE'});dirty=false;workspaceStarted=false;showLogin();await initialize()}catch(e){notice(e.message)}};
function collect(){const data={};for(const [key,id] of Object.entries(fields))data[key]=$(id).value;return {...data,attendees:[...attendees],conclusions:[...document.querySelectorAll('.conclusion-input')].map(n=>n.value.trim()).filter(Boolean),id:meetingId,revision,analysis_job_id:completedAnalysisId,transcript:transcriptComplete?transcript:''};}
function markDirty(){dirty=true;$('save-status').textContent='저장하지 않은 변경사항이 있습니다.';$('document-status').textContent=savedStatus==='confirmed'?'수정 중':'작성 중';$('document-status').className='status-badge neutral';$('source-count').textContent=$('source-text').value.length.toLocaleString()+'자';}
for(const id of Object.values(fields))$(id).addEventListener('input',markDirty);
function autoSize(node){node.style.height='auto';node.style.height=Math.min(250,Math.max(47,node.scrollHeight))+'px'}
function addConclusion(value='',notify=true){
 if(document.querySelectorAll('.conclusion-row').length>=100){notice('결론 및 추진사항은 최대 100개까지 입력할 수 있습니다.');return;}
 const row=el('div','','conclusion-row'),number=el('span'),input=el('textarea','','conclusion-input'),remove=el('button','×');
 input.value=String(value).replace(/[\r\n]+/g,' ');input.rows=1;input.maxLength=10000;input.placeholder='결론 또는 추진사항을 한 줄로 요약해 주세요';
 remove.type='button';remove.setAttribute('aria-label','결론 및 추진사항 삭제');
 input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing)e.preventDefault();});
 input.addEventListener('input',e=>{if(!e.isComposing&&/[\r\n]/.test(input.value))input.value=input.value.replace(/[\r\n]+/g,' ');autoSize(input);markDirty();});
 remove.onclick=()=>{row.remove();renumber();markDirty();};row.append(number,input,remove);$('conclusion-list').append(row);renumber();requestAnimationFrame(()=>autoSize(input));if(notify)markDirty();
}
function renumber(){document.querySelectorAll('.conclusion-row').forEach((row,i)=>{row.querySelector('span').textContent=String(i+1).padStart(2,'0');row.querySelector('textarea').setAttribute('aria-label',`결론 및 추진사항 ${i+1}`);});}
function setConclusions(items=[]){$('conclusion-list').replaceChildren();for(const item of items)addConclusion(item,false);if(!items.length)addConclusion('',false);}
$('add-conclusion').onclick=()=>addConclusion();
let summarizingConclusions=false;
async function summarizeConclusions(){
 if(busy||summarizingConclusions)return;if(!aiConfigured){$('ai-dialog').showModal();return;}
 const snapshot=collect();if(!snapshot.discussion.trim()&&!snapshot.source_text.trim()&&!snapshot.conclusions.length){notice('요약할 회의내용 또는 기존 결론을 입력해 주세요.');return;}
 const originalId=meetingId,originalSummary=JSON.stringify(snapshot.conclusions);
 summarizingConclusions=true;const button=$('summarize-conclusions');button.disabled=true;button.textContent='항목 요약 중…';$('conclusion-summary-status').textContent='결론 및 추진사항을 항목별 한 줄로 요약하고 있습니다.';
 try{const result=await api('/api/conclusions/summarize',{method:'POST',body:JSON.stringify({discussion:snapshot.discussion,source_text:snapshot.source_text,conclusions:snapshot.conclusions})});
  if(meetingId!==originalId||JSON.stringify(collect().conclusions)!==originalSummary||$('discussion').value!==snapshot.discussion||$('source-text').value!==snapshot.source_text){$('conclusion-summary-status').textContent='요약 중 내용이 변경되었습니다. 현재 내용을 유지했습니다. 다시 요약해 주세요.';return;}
  setConclusions(result.conclusions);markDirty();$('conclusion-summary-status').textContent='각 항목을 한 줄로 요약했습니다. 확인 후 저장해 주세요.';
 }catch(e){$('conclusion-summary-status').textContent=e.message;}finally{summarizingConclusions=false;button.disabled=busy;button.textContent='항목별 한 줄 요약';}
}
$('summarize-conclusions').onclick=summarizeConclusions;
function renderAttendees(){$('attendee-chips').replaceChildren();attendees.forEach((name,index)=>{const chip=el('span','','chip');chip.append(el('span',name));const remove=el('button','×');remove.type='button';remove.setAttribute('aria-label',name+' 삭제');remove.onclick=()=>{attendees.splice(index,1);renderAttendees();markDirty()};chip.append(remove);$('attendee-chips').append(chip)});$('attendee-count').textContent=attendees.length+'명'}
function addAttendee(){const names=$('attendee-input').value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean);if(!names.length)return;for(const name of names){if(!attendees.includes(name)&&attendees.length<200)attendees.push(name.slice(0,100))}$('attendee-input').value='';renderAttendees();markDirty();$('attendee-input').focus()}
$('add-attendee').onclick=addAttendee;$('attendee-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();addAttendee()}});
function selectFile(file){if(busy)return;if(file){if(file.size>100000000){notice('100 MB 이하의 파일을 선택해 주세요.');return}if(!/\.(mp3|m4a|wav|mp4|mpeg|mpga|webm)$/i.test(file.name)){notice('지원되는 녹음파일을 선택해 주세요.');return}}selectedFile=file||null;$('file-details').hidden=!file;$('file-name').textContent=file?`${file.name} · ${(file.size/1000000).toFixed(1)} MB`:'';$('file-label').textContent=file?'녹음파일 선택 완료':'녹음파일을 끌어오거나 선택하세요';if(!file)$('audio-file').value='';markDirty()}
$('audio-file').addEventListener('change',e=>selectFile(e.target.files[0]));$('remove-file').onclick=()=>selectFile(null);
for(const eventName of ['dragenter','dragover'])$('upload-zone').addEventListener(eventName,e=>{e.preventDefault();$('upload-zone').classList.add('dragover')});
for(const eventName of ['dragleave','drop'])$('upload-zone').addEventListener(eventName,e=>{e.preventDefault();$('upload-zone').classList.remove('dragover');if(eventName==='drop')selectFile(e.dataTransfer.files[0])});
function setBusy(value){busy=value;$('summarize-conclusions').disabled=value||summarizingConclusions;$('resume-analysis').disabled=value;$('refresh-analysis-history').disabled=value;for(const n of document.querySelectorAll('#analysis-history-list button'))n.disabled=value;for(const id of ['analyze','manual-format','new-meeting','list-new','confirm','save-draft','remove-file','audio-file'])$(id).disabled=value;$('analysis-progress').hidden=!value;$('analyze').querySelector('span').textContent=value?'분석 중…':'회의록 분석하기'}
function hasResult(){const d=collect();return d.discussion.trim()||d.notes.trim()||d.conclusions.length}
$('manual-format').onclick=()=>{if(hasResult()&&!confirm('현재 정리 내용을 입력 원문으로 바꿀까요?'))return;$('discussion').value=$('source-text').value;setConclusions();$('conclusion-summary-status').textContent='';$('notes').value='';$('step2').classList.add('active');markDirty();$('discussion').focus();notice('양식에서 내용을 직접 수정하고 확정해 주세요.')};
function setResumeControl(id,available,label='중단된 분석 이어하기'){
 activeAnalysisId=id||'';resumeAvailable=Boolean(available);
 $('resume-analysis').hidden=!available;$('resume-analysis').disabled=busy;
 $('resume-analysis').textContent=label;
}
function restoreAnalysisContext(status){
 const context=status.context||{};
 for(const [key,id] of Object.entries(fields)){if(key in context)$(id).value=context[key]??'';}
 if(context.attendees){attendees=[...context.attendees];renderAttendees();}
 $('source-text').value=status.source_text||'';
 selectedFile=null;$('audio-file').value='';$('file-details').hidden=true;
 $('file-label').textContent='녹음파일을 끌어오거나 선택하세요';
 if(status.transcript){transcript=status.transcript;transcriptComplete=status.transcript_complete!==false;showTranscript();}
 markDirty();
}
async function loadAnalysisHistory(){
 try{
  const data=await api('/api/analysis');$('analysis-history-list').replaceChildren();$('analysis-history-error').textContent='';
  if(!data.items.length){$('analysis-history-list').append(el('p','최근 분석 작업이 없습니다.','hint'));return;}
  for(const job of data.items){
   const row=el('div','','analysis-job-row');
   row.style.cssText='padding:12px 0;border-bottom:1px solid #e4eae7';
   const title=el('strong',job.title);const detail=el('p','','hint');
   const date=new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(job.created_at));
   const state={succeeded:'분석 완료',failed:'중단됨',interrupted:'중단 / 대기',running:'분석 중',queued:'대기 중'}[job.state]||job.state;
   detail.textContent=`${date} · ${state}${job.total_parts?` · ${job.completed_parts}/${job.total_parts} 구간 저장`:''}`;
   const controls=el('div','','analysis-job-actions');
   const button=el('button',job.state==='succeeded'?'결과 불러오기':job.resumable?'이어서 분석':'진행 상태 확인','btn secondary');
   button.type='button';button.disabled=busy;
   button.onclick=()=>openAnalysisJob(job.job_id,job.resumable);
   const remove=el('button','삭제','btn secondary danger');remove.type='button';remove.disabled=busy||job.state==='running';
   remove.onclick=async()=>{if(!confirm('이 분석 작업과 업로드한 녹음파일을 삭제할까요?'))return;try{await api('/api/analysis/'+job.job_id,{method:'DELETE'});if(activeAnalysisId===job.job_id){setResumeControl('',false);sessionStorage.removeItem('medparkAnalysisJob');}$('analysis-history-error').textContent='';await loadAnalysisHistory();notice('분석 작업을 삭제했습니다.');}catch(e){$('analysis-history-error').textContent=e.message;}};
   controls.append(button,remove);row.append(title,detail,controls);
   if(job.error)row.append(el('p',job.error,'error-message'));
   if(job.resume_unavailable_reason)row.append(el('p',job.resume_unavailable_reason,'hint'));
   $('analysis-history-list').append(row);
  }
 }catch(e){$('analysis-history-error').textContent=e.message;}
}
async function openAnalysisJob(id,resume=false){
 if(busy){notice('현재 분석 상태를 확인 중입니다.');return;}
 if(dirty&&!confirm('작성 중인 내용이 있습니다. 선택한 분석 작업을 불러올까요?'))return;
 try{
  const status=await api('/api/analysis/'+id);
  reset(false);restoreAnalysisContext(status);activeAnalysisId=id;
  sessionStorage.setItem('medparkAnalysisJob',id);location.hash='write';await route();
  if(resume&&status.resumable)await resumeAnalysisJob(id);else await pollAnalysis(id);
 }catch(e){$('analysis-error').textContent=e.message;}
}
async function resumeAnalysisJob(id=activeAnalysisId){
 if(!id||busy)return;
 setBusy(true);$('analysis-error').textContent='';
 $('analysis-progress').querySelector('span:last-child').textContent='저장된 구간에서 이어서 분석을 시작합니다…';
 try{
  const result=await api('/api/analysis/'+id+'/resume',{method:'POST',body:JSON.stringify({})});
  sessionStorage.setItem('medparkAnalysisJob',result.job_id);
  await pollAnalysis(result.job_id);
 }catch(e){$('analysis-error').textContent=e.message;setResumeControl(id,true);}
 finally{setBusy(false);}
}
$('resume-analysis').onclick=()=>resumeAnalysisJob();
$('refresh-analysis-history').onclick=loadAnalysisHistory;
$('analysis-history').addEventListener('toggle',()=>{if($('analysis-history').open)loadAnalysisHistory();});
$('analyze').onclick=async()=>{
 if(!selectedFile&&!$('source-text').value.trim()&&!transcript){notice('녹음파일 또는 실제 회의내용을 입력해 주세요.');$('source-text').focus();return;}
 if(!aiConfigured){$('ai-dialog').showModal();return;}
 if(resumeAvailable&&!confirm('보관된 작업이 있습니다. 처음부터 새로 분석할까요? 이어서 하려면 취소 후 이어하기를 눌러 주세요.'))return;
 if(hasResult()&&!confirm('분석이 완료되면 현재 정리 내용이 새 결과로 바뀝니다. 분석할까요?'))return;
 const form=new FormData(),data=collect();
 for(const key of ['title','category','meeting_date','duration','author','reporter','source_text'])form.append(key,data[key]);
 form.append('attendees_json',JSON.stringify(attendees));form.append('transcript',transcriptComplete?transcript:'');
 setResumeControl('',false);setBusy(true);completedAnalysisId='';
 $('analysis-progress').querySelector('span:last-child').textContent=selectedFile?'녹음파일을 업로드하고 있습니다…':'회의 내용을 전송하고 있습니다…';$('analysis-error').textContent='';
 try{if(selectedFile){const uploadId=await uploadAudio(selectedFile);form.append('audio_upload_id',uploadId);}const result=await api('/api/analyze',{method:'POST',body:form});activeAnalysisId=result.job_id;sessionStorage.setItem('medparkAnalysisJob',result.job_id);await pollAnalysis(result.job_id);}
 catch(e){$('analysis-error').textContent=e.message;setBusy(false);}
};
async function pollAnalysis(id,resuming=false){
 setBusy(true);activeAnalysisId=id;let disconnected=0;
 if(resuming)notice('저장된 분석 진행 상태를 확인하고 있습니다.');
 try{
  for(let attempt=0;attempt<4320;attempt++){
   let status;
   try{status=await api('/api/analysis/'+id);disconnected=0;}
   catch(e){
    if(e.status===401||e.status===403||e.status===404)throw e;
    disconnected++;
    $('analysis-progress').querySelector('span:last-child').textContent='연결 상태를 다시 확인하고 있습니다. 완료 구간은 서버에 저장됩니다.';
    if(disconnected>=12){setResumeControl(id,true,'진행 상태 확인 / 이어하기');throw new Error('연결이 원활하지 않습니다. 잠시 후 진행 상태 확인을 눌러 주세요.');}
    await new Promise(resolve=>setTimeout(resolve,5000));continue;
   }
   $('analysis-progress').querySelector('span:last-child').textContent=status.stage||'회의 내용을 분석하고 있습니다.';
   if(status.state==='succeeded'){
    completedAnalysisId=id;applyAnalysis(status.result);setResumeControl('',false);sessionStorage.removeItem('medparkAnalysisJob');return;
   }
   if(status.state==='failed'||status.state==='interrupted'){
    if(status.transcript){transcript=status.transcript;transcriptComplete=status.transcript_complete!==false;showTranscript();markDirty();}
    setResumeControl(id,status.resumable);
    $('analysis-error').textContent=(status.error||'분석이 중단되었거나 시작 대기 중입니다.')+' '+(status.resumable?`완료된 ${status.completed_parts}/${status.total_parts||'?'}개 구간을 보관했습니다. 이어하기를 눌러 주세요.`:status.resume_unavailable_reason||'');
    return;
   }
   await new Promise(resolve=>setTimeout(resolve,2500));
  }
  setResumeControl(id,true,'진행 상태 확인 / 이어하기');
  throw new Error('분석 상태 확인 시간이 지났습니다. 진행 상태 확인을 누르면 다시 연결합니다.');
 }catch(e){$('analysis-error').textContent=e.message;}
 finally{setBusy(false);loadAnalysisHistory();}
}
function showTranscript(){$('original-detail').hidden=!transcript;$('original-detail').querySelector('summary').textContent=transcriptComplete?'녹취 전사 확인':'녹취 전사 일부 확인 (미완료)';$('transcript-text').textContent=transcript;}
function applyAnalysis(response){const result=response.result;if(!$('title').value.trim())$('title').value=result.title||'';$('discussion').value=result.discussion||'';$('notes').value=result.notes||'';setConclusions(result.conclusions||[]);$('conclusion-summary-status').textContent='';if(response.transcript){transcript=response.transcript;transcriptComplete=true;showTranscript();if(!$('source-text').value.trim())$('source-text').value=transcript;selectedFile=null;$('audio-file').value='';$('file-details').hidden=true;$('file-label').textContent='녹음파일을 끌어오거나 선택하세요'}$('step2').classList.add('active');$('document-status').textContent='분석 완료';$('document-status').className='status-badge';markDirty();notice('분석이 완료됐습니다. 내용을 확인하고 확정해 주세요.')}
function validate(){if(!$('title').value.trim()){notice('회의 제목을 입력해 주세요.');$('title').focus();return false}if(!$('meeting-date').value){notice('회의일자를 선택해 주세요.');return false}return true;}
async function save(status){if(!validate())return;if(status==='confirmed'&&!hasResult()){notice('확정할 회의록 내용을 작성해 주세요.');return}if(status==='confirmed'&&!confirm('현재 회의록을 확정하여 목록에 저장할까요?'))return;const btn=status==='confirmed'?$('confirm'):$('save-draft');btn.disabled=true;try{const data=await api('/api/meetings',{method:'POST',body:JSON.stringify({...collect(),status})});meetingId=data.id;revision=data.revision;savedStatus=data.status;dirty=false;$('document-status').textContent=status==='confirmed'?'확정':'임시 저장';$('document-status').className='status-badge';$('save-status').textContent='저장 완료 · '+new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',timeStyle:'short'}).format(new Date());sessionStorage.setItem('medparkDraftId',data.id);notice(status==='confirmed'?(data.analysis_deleted===false?'회의록은 확정되었습니다. 분석 기록 정리는 서버에서 다시 처리됩니다.':data.analysis_deleted?'회의록을 확정하고 녹취·전사·입력 원문과 분석 기록을 삭제했습니다.':'회의록을 확정했습니다.'):'임시 저장했습니다.');if(status==='confirmed'){completedAnalysisId='';transcript='';transcriptComplete=true;selectedFile=null;showTranscript();$('source-text').value='';$('step3').classList.add('active');sessionStorage.removeItem('medparkDraftId');sessionStorage.removeItem('medparkAnalysisJob');location.hash='list';await route();}}catch(e){notice(e.message);$('save-status').textContent=e.message;}finally{btn.disabled=false}}
$('save-draft').onclick=()=>save('draft');$('confirm').onclick=()=>save('confirmed');
async function downloadCurrent(){if(!validate())return;const button=$('download');button.disabled=true;try{const response=await api('/api/download',{method:'POST',body:JSON.stringify(collect())});await saveBlob(response);}catch(e){notice(e.message)}finally{button.disabled=false}}
async function saveBlob(response){const blob=await response.blob();const disposition=response.headers.get('content-disposition')||'';const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i);const name=encoded?decodeURIComponent(encoded[1]):'MedPark_회의록.xlsx';const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),10000)}
$('download').onclick=downloadCurrent;
async function downloadId(id){try{await saveBlob(await api('/api/meetings/'+id+'/download'))}catch(e){notice(e.message)}}
async function downloadImageId(id,title='회의록'){
 try{await saveBlob(await api('/api/meetings/'+encodeURIComponent(id)+'/image.png'));}
 catch(e){notice(e.message||'PNG 이미지 파일을 만들지 못했습니다. 다시 시도해 주세요.');}
}
function reset(check=true){if(busy){notice('분석이 끝난 뒤 새 회의록을 작성해 주세요.');return false}if(check&&dirty&&!confirm('저장하지 않은 내용을 지우고 새 회의록을 작성할까요?'))return false;setResumeControl('',false);completedAnalysisId='';sessionStorage.removeItem('medparkAnalysisJob');for(const [key,id]of Object.entries(fields))$(id).value=key==='meeting_date'?today():key==='category'?'HR':'';meetingId=null;revision=0;attendees=[];savedStatus='draft';selectedFile=null;transcript='';transcriptComplete=true;showTranscript();$('attendee-input').value='';renderAttendees();$('audio-file').value='';$('file-details').hidden=true;$('file-label').textContent='녹음파일을 끌어오거나 선택하세요';setConclusions();$('conclusion-summary-status').textContent='';$('analysis-error').textContent='';$('source-count').textContent='0자';$('write-title').textContent='회의록 작성';$('document-status').textContent='작성 중';$('document-status').className='status-badge neutral';$('save-status').textContent='확정하면 회의록 목록에 저장됩니다.';$('save-draft').hidden=false;for(const id of ['step2','step3'])$(id).classList.remove('active');sessionStorage.removeItem('medparkDraftId');dirty=false;return true;}
function newMeeting(){if(reset()){location.hash='write';route();$('title').focus()}}
$('new-meeting').onclick=newMeeting;$('list-new').onclick=newMeeting;
function loadEditor(data){completedAnalysisId=data.analysis_job_id||'';for(const [key,id]of Object.entries(fields))$(id).value=data[key]??'';attendees=data.attendees||[];meetingId=data.id;revision=data.revision;savedStatus=data.status;transcript=data.transcript||'';transcriptComplete=true;showTranscript();renderAttendees();setConclusions(data.conclusions||[]);$('conclusion-summary-status').textContent='';$('source-count').textContent=$('source-text').value.length.toLocaleString()+'자';$('write-title').textContent=data.status==='confirmed'?'회의록 수정':'회의록 작성';$('document-status').textContent=data.status==='confirmed'?'확정':'임시 저장';$('document-status').className='status-badge';$('save-draft').hidden=data.status==='confirmed';$('save-status').textContent='저장된 회의록을 불러왔습니다.';$('step2').classList.add('active');$('step3').classList.toggle('active',data.status==='confirmed');dirty=false;}
async function route(){const isList=location.hash==='#list';$('write-view').hidden=isList;$('list-view').hidden=!isList;$('nav-write').classList.toggle('active',!isList);$('nav-list').classList.toggle('active',isList);$('breadcrumb').textContent=isList?'회의록 목록':'회의록 작성';if(isList)await loadList();}
window.addEventListener('hashchange',route);
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
async function loadList(){const sequence=++listSequence;$('list-error').textContent='';$('list-description').textContent='회의록을 불러오는 중입니다…';const params=new URLSearchParams({category:filterCategory,q:$('search-query').value,from:$('filter-from').value,to:$('filter-to').value,sort:$('sort-order').value,page});if(params.get('from')&&params.get('to')&&params.get('from')>params.get('to')){$('list-error').textContent='조회 시작일이 종료일보다 늦습니다.';return}try{const data=await api('/api/meetings?'+params);if(sequence!==listSequence)return;$('list-total').textContent=data.total;totalPages=Math.max(1,Math.ceil(data.total/data.page_size));$('page-label').textContent=page+' / '+totalPages;$('previous-page').disabled=page<=1;$('next-page').disabled=page>=totalPages;$('list-description').textContent=(filterCategory||'전체')+' 회의록 · '+data.total+'건';$('meeting-rows').replaceChildren();for(const item of data.items){const row=el('tr'),category=el('td'),badge=el('span',item.category,'category-pill');badge.dataset.category=item.category;category.append(badge);const title=el('td'),button=el('button',item.title,'row-title');button.onclick=()=>openMeeting(item.id);title.append(button);const names=item.attendees||[];const attendee=el('td',names.length?names.slice(0,2).join(', ')+(names.length>2?` 외 ${names.length-2}명`:''):'—');attendee.title=names.join(', ');const actions=el('td','','row-actions'),dl=el('button','','download-icon'),png=el('button','','download-icon');dl.setAttribute('aria-label',item.title+' 엑셀 다운로드');dl.title='엑셀 다운로드';dl.innerHTML='<svg><use href="/static/icons.svg#download"/></svg>';dl.onclick=()=>downloadId(item.id);png.setAttribute('aria-label',item.title+' PNG 이미지 다운로드');png.title='PNG 이미지 다운로드';png.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"></rect><circle cx="9" cy="10" r="2"></circle><path d="m4 18 5-5 4 4 3-3 4 4"></path></svg>';png.onclick=()=>downloadImageId(item.id,item.title);actions.append(dl,png);row.append(category,title,el('td',item.meeting_date),attendee,el('td',item.author||'—'),actions);$('meeting-rows').append(row)}$('list-empty').hidden=data.items.length>0;$('list-empty').querySelector('h3').textContent=params.get('q')||filterCategory||params.get('from')||params.get('to')?'검색된 회의록이 없습니다':'확정된 회의록이 없습니다';$('list-empty').querySelector('p').textContent=params.get('q')||filterCategory||params.get('from')||params.get('to')?'다른 키워드나 조회 조건으로 검색해 주세요.':'회의록을 작성하고 확정하면 이곳에서 볼 수 있습니다.';}catch(e){if(sequence!==listSequence)return;$('list-error').textContent=e.message;$('list-description').textContent='회의록을 불러오지 못했습니다.';$('meeting-rows').replaceChildren();$('list-empty').hidden=true;}}
$('search-form').onsubmit=e=>{e.preventDefault();page=1;loadList()};$('sort-order').onchange=()=>{page=1;loadList()};
$('category-tabs').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;filterCategory=b.dataset.category;for(const n of $('category-tabs').children)n.classList.toggle('active',n===b);page=1;loadList()});
$('reset-filter').onclick=()=>{$('search-query').value='';$('filter-from').value='';$('filter-to').value='';filterCategory='';page=1;$('sort-order').value='date_desc';for(const n of $('category-tabs').children)n.classList.toggle('active',n.dataset.category==='');loadList()};
$('previous-page').onclick=()=>{page=Math.max(1,page-1);loadList()};$('next-page').onclick=()=>{page=Math.min(totalPages,page+1);loadList()};
async function openMeeting(id){try{const d=await api('/api/meetings/'+id);selectedMeeting=d;const content=$('view-content');content.replaceChildren(el('h1',d.title));const meta=el('div','','minutes-meta');for(const [label,value,wide]of [['카테고리',d.category],['회의일자',d.meeting_date],['회의시간',d.duration?d.duration+'분':'—'],['부서 / 작성자',d.author||'—'],['보고자',d.reporter||'—'],['참석자',(d.attendees||[]).join(', ')||'—',true]]){const line=el('div','',wide?'wide':'');line.append(el('b',label),el('span',value));meta.append(line)}content.append(meta,el('h3','결론 및 추진사항'));const list=el('ol');for(const item of d.conclusions||[])list.append(minutesText('li',item));content.append(list.children.length?list:el('p','기재된 내용이 없습니다.'));content.append(el('h3','회의내용'),minutesText('p',d.discussion||'기재된 내용이 없습니다.'),el('h3','특이사항'),minutesText('p',d.notes||'기재된 내용이 없습니다.'));$('view-dialog').showModal();}catch(e){notice(e.message)}}
$('view-edit').onclick=()=>{if(dirty&&!confirm('현재 작성 중인 내용을 지우고 이 회의록을 수정할까요?'))return;if(busy){notice('분석이 끝난 뒤 수정해 주세요.');return}reset(false);loadEditor(selectedMeeting);sessionStorage.setItem('medparkDraftId',selectedMeeting.id);$('view-dialog').close();location.hash='write';route();};$('view-download').onclick=()=>{if(selectedMeeting)downloadId(selectedMeeting.id)};
for(const dialog of document.querySelectorAll('dialog')){dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close()});dialog.querySelectorAll('.close-dialog').forEach(b=>b.onclick=()=>dialog.close())}
$('ai-settings-button').onclick=()=>{$('ai-message').textContent='';$('api-key').value='';$('ai-dialog').showModal()};
$('ai-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;$('ai-message').textContent='연결을 확인하고 있습니다…';try{await api('/api/settings/ai',{method:'POST',body:JSON.stringify({api_key:$('api-key').value.trim()})});$('api-key').value='';setAI(true);$('ai-dialog').close();notice('AI 인증키를 확인하고 저장했습니다.')}catch(err){$('ai-message').textContent=err.message}finally{b.disabled=false}};
initialize();
