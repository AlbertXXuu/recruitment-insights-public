'use strict';
const el=id=>document.getElementById(id);
let registering=false,authBusy=false;
const registrationEnabled=document.body.dataset.registration!=='disabled';
function clearPasswords(){
  for(const id of ['password','register-password','register-password-confirm'])el(id).value='';
}
function showNotice(message,error=false){
  el('login-notice').textContent=message;
  el('login-notice').className=error?'notice error':'notice';
  el('login-notice').hidden=false;
}
function showForm(register){
  registering=register;clearPasswords();
  el('login-form').hidden=registering;el('register-form').hidden=!registering;
  el('login-title').textContent=registering?'创建普通端账号':'登录工作空间';
  el('login-description').textContent=registering?'搜索岗位、查看数据、保存收藏。':'登录后进入你的工作空间。';
  el('switch-auth').textContent=registering?'已有账号？返回登录':'没有账号？注册普通端';
  el('login-notice').hidden=true;
  el(registering?'register-name':'username').focus();
}
el('switch-auth').addEventListener('click',()=>{
  if(!registrationEnabled||authBusy)return;
  showForm(!registering);
});
async function submit(path,body,button){
  if(authBusy)return;
  authBusy=true;button.disabled=true;el('switch-auth').disabled=true;
  const original=button.textContent;button.textContent='处理中…';
  el('login-notice').hidden=true;
  try{
    let response,data;
    try{response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Request':'1'},body:JSON.stringify(body)});}
    catch{throw new Error('网络连接异常，请稍后重试。');}
    try{data=await response.json();}
    catch{throw new Error('服务暂时不可用，请稍后重试。');}
    if(!response.ok)throw new Error(typeof data?.error==='string'&&data.error.trim()?data.error:'服务暂时不可用，请稍后重试。');
    if(path.endsWith('/login')){
      if(!['candidate','admin','boss','hr'].includes(data?.role))throw new Error('服务暂时不可用，请稍后重试。');
      clearPasswords();window.location.href=data.role==='candidate'?'/public':'/enterprise';
    }
    else{showForm(false);el('username').value=body.username;showNotice('账号创建成功，请登录。');el('password').focus();}
  }catch(error){
    clearPasswords();showNotice(error.message,true);el(registering?'register-password':'password').focus();
  }finally{authBusy=false;button.disabled=false;button.textContent=original;el('switch-auth').disabled=false;}
}
el('login-form').addEventListener('submit',event=>{
  event.preventDefault();if(registering||authBusy)return;
  submit('/api/auth/login',{username:el('username').value,password:el('password').value},el('login-submit'));
});
el('register-form').addEventListener('submit',event=>{
  event.preventDefault();if(!registrationEnabled||!registering||authBusy)return;
  if(el('register-password').value!==el('register-password-confirm').value){
    showNotice('两次输入的密码不一致，请重新确认。',true);el('register-password-confirm').focus();return;
  }
  submit('/api/auth/register',{username:el('register-username').value,password:el('register-password').value,display_name:el('register-name').value},el('register-submit'));
});
