'use strict';
let csrf = '', me = null;
const el = id => document.getElementById(id);
async function request(path, options = {}) {
  const headers = {'Content-Type':'application/json', ...(options.body ? {'X-CSRF-Token':csrf}: {})};
  const response = await fetch(path, {credentials:'same-origin',...options,headers:{...headers,...options.headers}});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof result.detail === 'string' ? result.detail
      : (Array.isArray(result.detail) ? result.detail.map(item => `${item.loc.slice(1).join('.')}: ${item.msg}`).join('; ')
        : `Request failed (${response.status})`);
    const error = new Error(message); error.status = response.status; throw error;
  }
  return result;
}
function note(id,message,bad=false) {const node=el(id); node.className='message '+(bad?'error':'success');node.textContent=message;}
function text(tag,value,cls='') {const node=document.createElement(tag);node.textContent=value;node.className=cls;return node;}
function formData(form) {return Object.fromEntries(new FormData(form).entries());}
function meetingUrl(id) {return '/meeting/'+encodeURIComponent(id);}
async function loadFaculty() {
  const list=await request('/api/admin/faculty'); const field=el('facultySelect');field.replaceChildren(new Option('Select faculty',''));
  for(const person of list){field.add(new Option(`${person.display_name} (${person.username})`,person.username));}
}
async function loadZoomSetup() {
  const setup=await request('/api/admin/zoom-setup');
  el('zoomRedirect').value=setup.redirect_uri;
  el('copyRedirect').disabled=!setup.redirect_uri;
  el('zoomSetupMessage').textContent='Faculty sign in only to DocTutorials. Host access is authorized by your server. Enable user:read:token:admin, user:read:user:admin and webinar read access on your Server-to-Server OAuth app.';
  if(!setup.sdk_configured || !setup.api_configured)
    el('zoomSetupMessage').textContent+=' Zoom app credentials still need to be configured by the administrator.';
  const diagnosis=el('zoomDiagnosis');diagnosis.replaceChildren();
  const failures=await request('/api/admin/zoom-diagnostics');
  const latest=failures[0];
  if(latest && latest.code==='3038') {
    diagnosis.append(text('p',`Last join attempt: Zoom reports webinar ${latest.webinar_id} expired (3038). Reschedule it to a future date/time or assign a new webinar ID.`));
    const link=text('a','Open webinar in Zoom to reschedule','secondary');
    link.href='https://zoom.us/webinar/'+encodeURIComponent(latest.webinar_id);link.target='_blank';link.rel='noopener noreferrer';
    diagnosis.append(link);
  }
  el('zoomSetup').hidden=false;
}
async function loadOfficeRecorders() {
  const recorders=await request('/api/admin/recorders');
  const list=el('officeDevices');
  list.textContent=recorders.length?'':'No office Mac paired yet.';
  recorders.forEach((item,index)=>{
    if(index)list.append(' · ');
    list.append(`${item.name}: ${item.online?'online':'offline'} `);
    const remove=document.createElement('button');
    remove.type='button';remove.textContent='Remove';remove.style.cssText='padding:2px 8px;font-size:12px;margin:0';
    remove.addEventListener('click',async()=>{
      if(!confirm(`Remove "${item.name}"? New recordings will be saved on the remaining office recorder.`))return;
      try{await request('/api/admin/recorders/'+item.id,{method:'DELETE',headers:{'X-CSRF-Token':csrf}});await loadOfficeRecorders();}
      catch(error){note('officePairMessage',error.message,true);}
    });
    list.append(remove);
  });
  el('officePairing').hidden=false;
}
async function loadAssignments(autoEnter=false) {
  const rows=await request('/api/assignments'); const wrap=el('sessions');wrap.replaceChildren();wrap.className='';
  if(!rows.length){wrap.className='empty';wrap.textContent=me.role==='admin'?'No webinars scheduled yet. Create a faculty account and assign a webinar.':'No webinars assigned. Contact your administrator.';return;}
  for(const s of rows){const card=document.createElement('article');card.className='session';
    const content=document.createElement('div');content.append(text('div',`${s.course}  /  ${s.subject}`,'tiny muted'),text('h3',s.topic),text('div',`Webinar ${s.webinar_id} · ${s.faculty_name}`,'meta'));
    const actions=document.createElement('div');actions.className='hstack';actions.append(text('span',s.status,'badge'));
    if(me.role==='faculty' && s.status==='scheduled'){const button=text('button','Enter webinar','primary');button.onclick=()=>{window.location.assign(meetingUrl(s.id));};actions.append(button);}
    card.append(content,actions);wrap.append(card);
  }
  // After one faculty login, the sole assigned scheduled webinar opens automatically.
  // Multiple assignments require selection to avoid joining the wrong class.
  if(autoEnter && me.role==='faculty'){
    const live=rows.filter(s=>s.status==='scheduled');
    if(live.length===1) window.location.assign(meetingUrl(live[0].id));
    else if(live.length>1){
      const now=Date.now();
      const due=live.filter(s=>s.starts_at && !Number.isNaN(Date.parse(s.starts_at)) &&
        (now-Date.parse(s.starts_at)) >= -20*60*1000 && (now-Date.parse(s.starts_at)) <= 120*60*1000);
      if(due.length===1)window.location.assign(meetingUrl(due[0].id));
    }
  }
}
async function showApp(autoEnter=false) {
  me=await request('/api/me');csrf=me.csrf;el('who').textContent=me.display_name+' · '+me.role;
  el('zoomSetup').hidden=true;el('officePairing').hidden=true;
  el('roleBadge').textContent=me.role==='admin'?'Administration':'Faculty';
  el('heading').textContent=me.role==='admin'?'Live session administration':'Your live sessions';
  el('adminGrid').hidden=me.role!=='admin';
  if(me.role==='admin') {await loadZoomSetup();await loadFaculty();await loadOfficeRecorders();}
  await loadAssignments(autoEnter && me.role==='faculty');
  el('auth').hidden=true;el('dashboard').hidden=false;el('logout').hidden=false;
}
el('loginForm').addEventListener('submit',async event=>{
  event.preventDefault(); const btn=event.currentTarget.querySelector('button');btn.disabled=true;
  try{const info=await request('/api/login',{method:'POST',body:JSON.stringify(formData(event.currentTarget))});csrf=info.csrf;await showApp(true);}
  catch(e){note('loginMessage',e.message,true)}finally{btn.disabled=false;}
});
el('facultyForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;
  try{const data=formData(event.currentTarget);if(!data.zoom_user_id.trim())data.zoom_user_id=data.email;await request('/api/admin/faculty',{method:'POST',body:JSON.stringify(data)});note('facultyMessage','Faculty account created. Assign a webinar on the right.');event.target.reset();await loadFaculty();}
  catch(e){note('facultyMessage',e.message,true)}finally{button.disabled=false;}
});
el('assignForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;
  try{const data=formData(event.currentTarget);if(data.starts_at)data.starts_at=new Date(data.starts_at).toISOString();await request('/api/admin/assignments',{method:'POST',body:JSON.stringify(data)});note('assignMessage','Webinar assigned successfully. Faculty can now log in.');event.target.reset();await loadAssignments();}
  catch(e){note('assignMessage',e.message,true)}finally{button.disabled=false;}
});
el('copyRedirect').addEventListener('click',async()=>{
  try{await navigator.clipboard.writeText(el('zoomRedirect').value);note('zoomSetupMessage','Optional callback URL copied. Faculty access does not require OAuth sign-in.');}
  catch{el('zoomRedirect').focus();el('zoomRedirect').select();note('zoomSetupMessage','Select and copy the redirect URL shown above.');}
});
el('pairOffice').addEventListener('click',async()=>{
  const button=el('pairOffice');button.disabled=true;
  try{
    const paired=await request('/api/admin/recorders',{method:'POST',body:JSON.stringify({name:'Office Mac '+new Date().toLocaleString()})});
    el('officeCode').value=paired.token;el('officeCodePanel').hidden=false;
    el('officeAppUrl').value=location.origin;
    await loadOfficeRecorders();
  }catch(error){note('officePairMessage',error.message,true);}finally{button.disabled=false;}
});
el('refresh').addEventListener('click',()=>loadAssignments().catch(e=>alert(e.message)));
el('logout').addEventListener('click',async()=>{try{await request('/api/logout',{method:'POST',body:'{}'});}finally{window.location.assign('/');}});
showApp(false).catch(error=>{
  me=null;csrf='';el('auth').hidden=false;el('dashboard').hidden=true;el('logout').hidden=true;
  el('who').textContent='';el('zoomSetup').hidden=true;el('officePairing').hidden=true;
  if(error.status!==401)note('loginMessage',error.message,true);
});
