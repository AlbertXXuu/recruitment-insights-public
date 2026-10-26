'use strict';
const byId=id=>document.getElementById(id);
const labels={pending:'等待审核',approved:'已通过',rejected:'已驳回',withdrawn:'已撤回'};
let application=null,reviewing=null,page=1,busy=false,editing=false;
const adminPage=location.pathname==='/enterprise-review';
function message(text,error=false){byId('message').textContent=text;byId('message').className=error?'notice error':'notice';byId('message').hidden=false;}
async function api(path,body){
  const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-Local-Request':'1'},body:JSON.stringify(body)}:{});
  let data;try{data=await response.json();}catch{throw new Error('服务暂时不可用，请稍后重试');}
  if(!response.ok){const e=new Error(data.error||'请求未完成');e.status=response.status;throw e;}
  return data;
}
async function action(fn){
  if(busy)return;busy=true;
  const buttons=[...document.querySelectorAll('button')],states=buttons.map(b=>b.disabled);buttons.forEach(b=>b.disabled=true);
  try{await fn();}catch(e){message(e.message,true);}finally{busy=false;buttons.forEach((b,i)=>b.disabled=states[i]);}
}
function fields(row){
  const dl=document.createElement('dl');
  for(const [label,value] of [['企业',row.company_name],['状态',labels[row.status]],['联系人',row.contact_name],['联系方式',row.contact],['公开介绍',row.website||'未提供'],['申请说明',row.description],['审核意见',row.review_reason||'暂无'],['更新于',new Date(row.updated_at*1000).toLocaleString()]]){
    const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;dl.append(dt,dd);
  }return dl;
}
function renderHistory(target,rows){
  target.replaceChildren();for(const event of rows||[]){const row=JSON.parse(event.snapshot),p=document.createElement('p');p.textContent=`${new Date(event.created_at*1000).toLocaleString()} · ${labels[row.status]} · 第 ${row.version} 版\n${row.company_name}${row.review_reason?'\n'+row.review_reason:''}`;target.append(p);}
}
async function loadMine(){
  application=await api('/api/enterprise-application');
  if(!application){message('当前账号没有企业入驻申请。如需申请，请退出后使用独立企业工作账号。');return;}
  byId('registration').hidden=true;byId('application-status').hidden=false;
  byId('status-content').replaceChildren(fields(application));renderHistory(byId('history'),application.history);
  byId('withdraw').hidden=application.status!=='pending';byId('edit').hidden=!['rejected','withdrawn'].includes(application.status);
  byId('enter-workspace').hidden=application.status!=='approved';
}
async function loadQueue(){
  const data=await api('/api/admin/enterprise-applications?page='+page);byId('queue').replaceChildren();
  for(const row of data.items){
    const card=document.createElement('article');card.className='panel';const title=document.createElement('h3');title.textContent=row.company_name;
    const info=document.createElement('p');info.textContent=`${labels[row.status]} · ${row.display_name}（${row.username}）`;
    const button=document.createElement('button');button.className='secondary';button.textContent=row.status==='pending'?'查看并审核':'查看记录';
    button.addEventListener('click',()=>action(async()=>{
      reviewing=await api('/api/admin/enterprise-applications/'+row.id);byId('review-detail').replaceChildren(fields(reviewing));
      const history=document.createElement('details'),summary=document.createElement('summary'),content=document.createElement('div');summary.textContent='历史记录';renderHistory(content,reviewing.history);history.append(summary,content);byId('review-detail').append(history);
      byId('review-reason').value='';byId('review-reason').disabled=reviewing.status!=='pending';byId('review-error').textContent='';
      for(const b of byId('review-form').querySelectorAll('[name="decision"]'))b.hidden=reviewing.status!=='pending';
      byId('review-dialog').showModal();
    }));card.append(title,info,button);byId('queue').append(card);
  }
  if(!data.items.length)byId('queue').textContent='暂无企业申请。';
  byId('page-label').textContent=`第 ${page} 页 · 共 ${data.total} 条`;byId('previous').hidden=page<=1;byId('next').hidden=page*20>=data.total;
}
byId('application-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  const form=event.currentTarget||byId('application-form');const body=Object.fromEntries(new FormData(form));body.dedicated_account=form.elements.dedicated_account.checked;
  try{
    if(!editing&&body.password!==body.confirmation)throw new Error('两次密码输入不一致');
    if(editing){await api('/api/enterprise-application',{...body,action:'resubmit',version:application.version});editing=false;await loadMine();message('申请已重新提交。');}
    else{await api('/api/auth/register-enterprise',body);form.reset();byId('registration').hidden=true;message('申请已提交。请点击右上角“返回登录”，使用刚创建的工作账号查看审核状态。');}
  }finally{form.elements.password.value='';form.elements.confirmation.value='';}
});});
byId('withdraw').addEventListener('click',()=>action(async()=>{await api('/api/enterprise-application',{action:'withdraw',version:application.version});await loadMine();message('已撤回，可以修改后重新提交。');}));
byId('edit').addEventListener('click',()=>{
  editing=true;const form=byId('application-form');byId('account-fields').hidden=true;byId('account-fields').disabled=true;
  for(const key of ['company_name','contact_name','contact','website','description'])form.elements[key].value=application[key];
  form.elements.dedicated_account.checked=false;byId('registration').hidden=false;byId('submit-application').textContent='重新提交申请';form.elements.company_name.focus();
});
byId('review-form').addEventListener('submit',event=>{event.preventDefault();const decision=event.submitter?.value;action(async()=>{
  if(!decision||!reviewing)return;
  try{await api('/api/admin/enterprise-applications/review',{application_id:reviewing.id,version:reviewing.version,decision,reason:byId('review-reason').value});byId('review-dialog').close();await loadQueue();message(decision==='approved'?'已开通企业工作台。':'已驳回，申请人可以查看原因并重新提交。');}
  catch(e){byId('review-error').textContent=e.message;}
});});
byId('close-review').addEventListener('click',()=>byId('review-dialog').close());
byId('refresh-status').addEventListener('click',()=>action(loadMine));byId('refresh-queue').addEventListener('click',()=>action(loadQueue));
byId('previous').addEventListener('click',()=>action(async()=>{page--;await loadQueue();}));byId('next').addEventListener('click',()=>action(async()=>{page++;await loadQueue();}));
action(async()=>{
  let user;try{user=await api('/api/auth/me');}catch(e){if(e.status!==401)throw e;}
  if(adminPage){if(user?.role!=='admin')throw new Error('需要管理员账号');byId('heading').textContent='企业入驻审核';byId('intro').textContent='核对申请信息，开通企业工作台。审核结果与历史记录会保留。';byId('back-link').textContent='返回数据总览';byId('back-link').href='/enterprise';byId('review-panel').hidden=false;await loadQueue();}
  else if(user){byId('heading').textContent='企业入驻进度';byId('back-link').textContent='退出当前账号';byId('back-link').href='#';byId('back-link').addEventListener('click',event=>{event.preventDefault();action(async()=>{await api('/api/auth/logout',{});location.href='/join';});});await loadMine();}
  else byId('registration').hidden=false;
});
