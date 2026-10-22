'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'overview', page:1, duplicatePage:1, managementPage:1, managementSequence:0, companyEditorSequence:0, resumeSequence:0, resumeBusy:false, skill:'', stats:null, sequence:0, noticeKind:'action'};
const names = {overview:['数据总览','数据总览','查看当前样本的地区、岗位、技能与招聘要求分布。'],explore:['岗位探索','岗位探索','筛选岗位，查看招聘要求、来源和原始描述。'],import:['数据接入','数据接入','接入公开招聘数据，或导入已整理的 CSV 文件。'],provenance:['更新记录','更新记录','查看数据更新结果、异常记录与关键操作。']};
const sourceLabel = value => ({'greenhouse:cloudflare':'Cloudflare','greenhouse:datadog':'Datadog','greenhouse:stripe':'Stripe','greenhouse:figma':'Figma','ashby:openai':'OpenAI','lever:palantir':'Palantir','tencent:public-cn':'腾讯'})[value] || value;
function notice(message, error=false, kind='action'){state.noticeKind=kind;$('notice').textContent=message;$('notice').className='notice'+(error?' error':'');$('notice').hidden=false;}
async function api(path, body){const response=await fetch(path, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Local-Request':'1'},body:JSON.stringify(body)});const data=await response.json();if(response.status===401)window.location.assign('/login');if(!response.ok){const error=new Error(data.error||'请求失败');error.status=response.status;throw error;}return data;}
function filters(){const params=new URLSearchParams();for(const id of ['dataset','scope','q','location','category','education','experience','source'])if($(id).value)params.set(id,$(id).value);if(state.skill)params.set('skill',state.skill);return params;}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'暂无';}
function salaryLabel(job){
  const original=String(job.salary_text||'').trim();
  if(original)return original;
  if(job.salary_status==='有效'&&Number.isFinite(job.salary_low)&&Number.isFinite(job.salary_high)){
    const amount=value=>value.toLocaleString('zh-CN',{maximumFractionDigits:2});
    return `${amount(job.salary_low)}–${amount(job.salary_high)} ${job.currency} / 月（规范值）`;
  }
  return '未披露';
}
function switchView(view){if(view==='resume'&&state.user?.role!=='candidate')return;state.view=view;for(const el of document.querySelectorAll('.view'))el.hidden=el.id!==view;for(const el of document.querySelectorAll('.nav')){el.classList.toggle('active',el.dataset.view===view);el.setAttribute('aria-current',el.dataset.view===view?'page':'false');}const [,title,description]=names[view];$('page-title').textContent=title;$('page-description').textContent=description;const scoped=['overview','explore','quality'].includes(view);document.querySelector('.scope-bar').hidden=!scoped;$('scope-note').hidden=!scoped;refreshView().catch(e=>notice(e.message,true));}
function bars(id, items, field){const list=['education','experience'].includes(field)?items:items.slice(0,7);const max=Math.max(...list.map(x=>x.count),1);$(id).innerHTML=list.length?list.map(item=>`<button class="bar-row${/未知|未披露|未发现明确要求|待核验|待复核/.test(item.name)?' is-neutral':''}" data-field="${field}" data-value="${escapeHtml(item.name)}" title="${escapeHtml(item.name)}：${item.count} 条记录"><span class="bar-label">${escapeHtml(item.name)}</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${item.count/max*100}%"></span></span><span class="bar-count">${item.count}</span></button>`).join(''):'<div class="empty"><strong>当前筛选下暂无可用数据</strong><span>可清空筛选，或选择课程模拟数据查看样本。</span></div>';}
function renderSalary(){const data=state.stats?.salaries.find(x=>x.currency===$('currency').value);if(!data)return;$('salary-summary').innerHTML=`<div class="salary-kpi">${data.median===null?'—':data.median.toLocaleString('zh-CN',{maximumFractionDigits:0})}<small>月薪区间中点的中位数 · ${data.valid} 个有效样本</small></div>`;if(!data.valid){$('salary-chart').className='empty';$('salary-chart').textContent='该币种暂无可比较薪资；缺失不按 0 处理。';return;}$('salary-chart').className='columns';const max=Math.max(...data.bins.map(x=>x.count),1);$('salary-chart').innerHTML=data.bins.map(x=>`<div class="column"><span>${x.count}</span><div class="column-fill" style="height:${x.count/max*85}px"></div><span class="column-label">${x.name}</span></div>`).join('');}
function renderStats(stats){
  state.stats=stats;
  const valid=stats.salaries.reduce((n,x)=>n+x.valid,0);
  const metrics=[['岗位记录',stats.total,'当前有效来源记录'],['地区标签',stats.locations.length,'按来源原始地区统计'],['技能标签',stats.skills.length,'从岗位原文识别'],['薪资样本',valid,'可比较的薪资记录']];
  $('metrics').innerHTML=metrics.map(([label,value,note])=>`<div class="metric"><span class="metric-label">${label}</span><div class="metric-value">${value.toLocaleString()}</div><span class="metric-note">${note}</span></div>`).join('');
  for(const [field,id] of [['location','locations'],['category','categories'],['skill','skills'],['education','education'],['experience','experience']])bars(({location:'location-chart',category:'category-chart',skill:'skills-chart',education:'education-chart',experience:'experience-chart'})[field],stats[id],field);
  renderSalary();
  const demo=$('dataset').value==='demo',sources=stats.sources||[];
  const sourceNames=sources.slice(0,3).map(s=>sourceLabel(s.name)).join('、');
  const sourceText=sources.length?`当前筛选包含 ${sources.length} 个来源：${sourceNames}${sources.length>3?'等':''}。`:'当前筛选没有来源记录。';
  $('scope-note').className='scope-note'+(demo?' demo-note':'');
  $('scope-note').textContent=`${demo?'模拟数据 · 仅用于功能测试':'真实 / 自有样本'} · ${stats.total.toLocaleString()} 条岗位 · ${sources.length} 个来源`;
  $('source-method').textContent=sourceText+(demo?'这是按随机种子生成的模拟分布，仅用于测试。':'每条为来源记录，跨来源可能同岗；地区保留原标签，样本不能代表整个招聘市场。');
  for(const field of ['education','experience']){
    const c=stats.requirement_coverage?.[field];
    $(field+'-coverage').textContent=c?`来源已明确 ${c.source} · 已核验 ${c.confirmed} · 待核验 ${c.pending} · 待复核 ${c.stale}`:'';
  }
  $('last-update').textContent='当前样本最近观测：'+date(stats.last_seen);
  $('skill-filter').innerHTML=state.skill?`<button class="pill" id="clear-skill">技能：${escapeHtml(state.skill)} · 清除</button>`:'';
  const advanced=document.querySelector('.filter-details');
  if(advanced&&['education','experience','source'].some(id=>$(id).value))advanced.open=true;
}
function renderJobs(data){$('job-count').textContent=`${data.total} 条`;$('jobs').innerHTML=data.items.length?data.items.map(j=>`<tr><td><strong>${escapeHtml(j.title)}</strong><small>${escapeHtml(j.company)}</small></td><td>${escapeHtml(j.location)}</td><td>${escapeHtml(salaryLabel(j))}${j.salary_text?`<small>${escapeHtml(j.currency)}</small>`:''}</td><td>${j.skills.slice(0,4).map(s=>`<span class="tag">${escapeHtml(s)}</span>`).join('')||'—'}</td><td><small>${escapeHtml(j.source)}</small>${j.is_demo?'<span class="badge partial">模拟</span>':''}</td><td><button class="detail-button" data-job="${j.id}" aria-label="查看 ${escapeHtml(j.title)} 详情">查看 </button></td></tr>`).join(''):'<tr><td colspan="6"><div class="empty">没有匹配的岗位。可清空筛选，或选择课程模拟数据查看样本。</div></td></tr>';$('page-status').textContent=`第 ${data.page} / ${Math.max(1,Math.ceil(data.total/15))} 页 · 共 ${data.total} 条`;$('prev').disabled=state.page<=1;$('next').disabled=state.page*15>=data.total;}
async function load(options=false){
  const sequence=++state.sequence, dataset=$('dataset').value, scope=$('scope').value;
  const optionKey=dataset+':'+scope;
  const needOptions=options||state.optionsDataset!==optionKey;
  const optionFields=['location','category','education','experience','source'];
  if(needOptions){for(const id of optionFields)$(id).disabled=true;}
  const params=filters();
  const requests=[api('/api/overview?'+params+'&page='+state.page+'&include_options='+(needOptions?'1':'0'))];
  const qualityIndex=requests.length;
  if(state.view==='quality')requests.push(api('/api/quality?'+params+'&duplicate_page='+state.duplicatePage));
  try{
  const responses=await Promise.all(requests);
  const {stats,jobs,options:opts}=responses[0];
  if(sequence!==state.sequence)return;
  if(opts){
    for(const [key,values] of Object.entries(opts)){
      const selected=$(key).value,first=$(key).options[0].textContent;
      $(key).innerHTML=`<option value="">${first}</option>`+values.map(v=>`<option value="${escapeHtml(v)}">${escapeHtml(key==='source'?sourceLabel(v):v)}</option>`).join('');
      $(key).value=values.includes(selected)?selected:'';
      $(key).disabled=false;
    }
    state.optionsDataset=optionKey;
  }
  renderStats(stats);renderJobs(jobs);
  if(state.view==='quality'&&responses[qualityIndex])renderQuality(responses[qualityIndex]);
  if(sequence===state.sequence&&state.noticeKind==='load'){$('notice').hidden=true;$('notice').textContent='';state.noticeKind='action';}
  }finally{
    if(sequence===state.sequence&&needOptions)for(const id of optionFields)$(id).disabled=false;
  }
}
function reload(options=false){
  const request=load(options),sequence=state.sequence;
  return request.catch(e=>{if(sequence===state.sequence)notice('读取失败：'+e.message,true,'load');});
}
function clear(){for(const id of ['q','location','category','education','experience','source'])$(id).value='';state.skill='';state.page=1;state.duplicatePage=1;}
async function detail(id){
  const request=state.detailRequest=(state.detailRequest||0)+1;
  try{
    const row=await api('/api/jobs/'+id);
    if(row.can_review&&!state.reviewOptions)state.reviewOptions=await api('/api/review-options');
    if(request!==state.detailRequest)return;
    renderDetail(row);
    if(!$('detail').open)$('detail').showModal();
  }catch(e){if($('detail').open)detailNotice(e.message,true);else notice(e.message,true);}
}
function renderDetail(row,message=''){
    state.detail=row;
    const j=row.payload,versions=row.revisions||[];
    const experience=j.experience==='年限范围'?(experienceRange(row.requirements?.experience)||j.experience):j.experience;
    $('detail-heading').textContent=row.can_review?'岗位详情与字段核验':'岗位详情';
    $('detail-content').innerHTML=`<div class="detail-title"><h2>${escapeHtml(j.title)}</h2><p class="body-text">${escapeHtml(j.company)} · ${escapeHtml(j.location)} ${row.is_demo?'<span class="badge">模拟数据</span>':''}</p></div>
      <div id="detail-notice" class="notice review-notice" role="status" aria-live="polite" ${message?'':'hidden'}>${escapeHtml(message)}</div>
      <div class="detail-grid">${[[j.salary_text?'薪资原文':'薪资说明',salaryLabel(j)],['学历',j.education],['经验',experience]].map(([k,v])=>`<div><b>${k}</b>${escapeHtml(v)}</div>`).join('')}</div>
      <p>${j.skills.map(s=>`<span class="tag">${escapeHtml(s)}</span>`).join('')}</p>
      <div class="detail-actions">${state.user?.role==='candidate'?`<button class="primary" data-save-favorite="${row.id}">收藏岗位</button>`:''}
      ${j.source_url?`<a href="${escapeHtml(j.source_url)}" target="_blank" rel="noreferrer">查看来源页面</a>`:''}</div>
      <section class="detail-description"><h3>岗位描述</h3><div class="description">${escapeHtml(j.description||'未提供')}</div></section>
      <details class="detail-metadata"><summary>来源与数据说明${row.quality_issues.length?' · 有字段待关注':''}</summary><div class="detail-grid">${[['数据类型',row.is_demo?'模拟 / 测试数据':'真实 / 自有数据'],['来源',row.source],['最近观测',date(row.last_seen)],['薪资处理',j.salary_status]].map(([k,v])=>`<div><b>${k}</b>${escapeHtml(v)}</div>`).join('')}</div>${row.quality_issues.length?`<p class="body-text">${row.quality_issues.map(i=>escapeHtml(i.message)).join('；')}</p>`:''}</details>
      ${['education','experience'].map(field=>renderRequirement(row,field)).join('')}
      ${row.raw?`<details><summary>原始记录（本企业或管理员可见）</summary><pre>${escapeHtml(JSON.stringify(row.raw,null,2))}</pre></details><details><summary>内容版本 · ${versions.length} 次</summary>${versions.map(r=>`<p>批次 #${r.run_id} · ${date(r.observed_at)}</p><pre>${escapeHtml(JSON.stringify(r.payload,null,2))}</pre>`).join('')}</details>`:''}`;
}
const requirementLabels={education:'学历',experience:'经验'};
const reviewStatusLabels={source:'来源结构化字段',candidate:'提取建议 · 待核验',unresolved:'尚无结论 · 待核验',confirmed:'人工已核验',stale:'来源已变化 · 待复核'};
function selectOptions(values,selected){return values.map(value=>`<option value="${escapeHtml(value)}"${value===selected?' selected':''}>${escapeHtml(value)}</option>`).join('');}
function experienceRange(r){
  if(!r||(r.min_years==null&&r.max_years==null))return '';
  if(r.min_years!=null&&r.max_years!=null)return r.min_years===r.max_years?`${r.min_years} 年`:`${r.min_years}–${r.max_years} 年`;
  return r.min_years!=null?`至少 ${r.min_years} 年`:`最多 ${r.max_years} 年`;
}
function renderRequirement(row,field){
  const r=row.requirements?.[field];
  if(!r)return '';
  const title=requirementLabels[field],candidate=['candidate','unresolved','stale'].includes(r.status);
  const summary=`${candidate?'待确认结论':'当前结论'}：${r.value||'未知'}${r.condition?' · '+r.condition:''}`;
  const alternatives=Array.isArray(r.alternatives)?r.alternatives:[];
  const range=field==='experience'?experienceRange(r):'';
  let form='';
  if(row.can_review&&state.reviewOptions){
    const values=state.reviewOptions[field+'_values']||[],conditions=state.reviewOptions.conditions||[];
    const value=values.includes(r.value)?r.value:'';
    const condition=conditions.includes(r.condition)?r.condition:'';
    const levels=values.filter(v=>!/未知|未披露|不限|待|多种/.test(v));
    form=`<form class="review-form" data-review-field="${field}">
      <div class="form-grid"><label>规范结论<select id="review-${field}-value" required><option value=""${value?'':' selected'}>请选择规范结论</option>${selectOptions(values,value)}</select></label><label>要求性质<select id="review-${field}-condition" required><option value=""${condition?'':' selected'}>请选择要求性质</option>${selectOptions(conditions,condition)}</select></label></div>
      ${field==='education'?`<fieldset class="review-alternatives" id="review-education-alternatives" ${value==='多种学历选择'?'':'hidden'}><legend>可接受的学历（按原文选择）</legend><div class="form-flags">${levels.map(v=>`<label class="check"><input type="checkbox" name="education-alternative" value="${escapeHtml(v)}" ${alternatives.includes(v)?'checked':''}>${escapeHtml(v)}</label>`).join('')}</div></fieldset>`:`<div class="form-grid" id="review-experience-range" ${value==='年限范围'?'':'hidden'}><label>明确年限下限（可留空）<input id="review-experience-min" type="number" min="0" max="60" step="any" value="${r.min_years??''}"></label><label>明确年限上限（可留空）<input id="review-experience-max" type="number" min="0" max="60" step="any" value="${r.max_years??''}"></label></div>`}
      <label>原文依据<textarea id="review-${field}-evidence" rows="3" maxlength="2000" placeholder="从上方岗位描述或原始字段中复制支持结论的原句">${escapeHtml(r.evidence||'')}</textarea></label>
      <p class="review-help">原文依据须能在本条记录中找到。没有明确要求时选择“未知”及“未发现明确要求”，依据可留空；不要把未知改成不限。</p>
      <div class="card-actions"><button class="primary" type="submit">确认${title}结论</button>${['confirmed','stale'].includes(r.status)?`<button class="secondary" type="button" data-revoke-field="${field}">撤销人工核验</button>`:''}</div>
    </form>`;
    form=`<details class="review-controls"><summary>${r.status==='confirmed'?'调整已确认的结论':'核验'+title+'结论'}</summary>${form}</details>`;
  }
  return `<section class="review-section"><div class="panel-head"><h3>${title}${row.can_review?'核验':'要求'}</h3><span class="badge ${escapeHtml(r.status)}">${escapeHtml(reviewStatusLabels[r.status]||r.status)}</span></div><p class="body-text">${escapeHtml(summary)}${alternatives.length?'<br>可接受：'+alternatives.map(escapeHtml).join('、'):''}${range?'<br>'+escapeHtml(candidate?'待核验年限：':'明确年限：')+escapeHtml(range):''}</p>${r.evidence?`<blockquote class="review-evidence">${escapeHtml(r.evidence)}</blockquote>`:''}${candidate?'<p class="review-help">此结果尚未确认，不作为确定的学历或经验要求参与统计。</p>':''}${r.reviewed_at?`<p class="review-help">最近核验：${date(r.reviewed_at)}</p>`:''}${form}</section>`;
}
function detailNotice(message,error=false,reloadRequired=false){
  const element=$('detail-notice');
  if(!element){notice(message,error);return;}
  element.className='notice review-notice'+(error?' error':'');element.hidden=false;
  element.innerHTML=escapeHtml(message)+(reloadRequired?'<br><button class="secondary" type="button" data-reload-detail>重新载入当前记录</button>':'');
  element.scrollIntoView({block:'nearest'});
}
async function saveReview(field,action){
  if(state.reviewBusy)return;
  const row=state.detail,r=row?.requirements?.[field];
  if(!r||!row.can_review)return;
  const body={job_id:row.id,field,action,source_hash:r.source_hash,version:r.version};
  if(action==='confirm'){
    Object.assign(body,{value:$('review-'+field+'-value').value,condition:$('review-'+field+'-condition').value,evidence:$('review-'+field+'-evidence').value.trim(),alternatives:[]});
    if(field==='education'&&body.value==='多种学历选择')body.alternatives=[...$('review-education-alternatives').querySelectorAll('input:checked')].map(input=>input.value);
    if(field==='experience')for(const [key,id] of [['min_years','min'],['max_years','max']])body[key]=body.value!=='年限范围'||$('review-experience-'+id).value===''?null:Number($('review-experience-'+id).value);
  }
  state.reviewBusy=true;
  for(const button of $('detail-content').querySelectorAll('.review-form button'))button.disabled=true;
  try{
    const updated=await api('/api/reviews',body);
    if(state.detail?.id===row.id)renderDetail(updated,action==='revoke'?'已撤销人工核验，统计已恢复到当前来源与待核验状态。':'核验已保存，图表与筛选将使用已确认结论。');
    await load(true);
  }catch(error){detailNotice(error.status===409?'这条记录或核验结果已被更新。请重新载入，再对照最新原文提交。':error.message,true,error.status===409);}
  finally{state.reviewBusy=false;for(const button of $('detail-content').querySelectorAll('.review-form button'))button.disabled=false;}
}
async function loadRuns(){const rows=await api('/api/runs');$('runs').innerHTML=rows.length?rows.map(r=>`<div class="run"><div class="run-header"><span>#${r.id} ${escapeHtml(r.label)} <span class="badge ${r.status}">${{success:'成功',partial:'部分成功',failed:'失败',running:'进行中'}[r.status]||r.status}</span></span><small>${date(r.started_at)}</small></div><div class="run-counts">${r.result.error?escapeHtml(r.result.error):`共 ${r.result.total} 行 · 新增 ${r.result.inserted} · 修改 ${r.result.updated} · 未变 ${r.result.unchanged} · 无效 ${r.result.rejected} · 批内重复折叠 ${r.result.duplicated??0} · 冲突退回 ${r.result.conflicts??0}（已计入无效） · 停用 ${r.result.deactivated}`}</div>${r.result.errors?.length?`<details><summary>查看 ${r.result.errors.length} 条无效行及原因</summary><pre>${escapeHtml(JSON.stringify(r.result.errors,null,2))}</pre></details>`:''}</div>`).join(''):'<div class="empty">还没有更新记录</div>';}
async function action(button, task){button.disabled=true;const original=button.textContent;button.textContent='处理中…';try{await task();}catch(e){notice(e.message,true);}finally{button.disabled=false;button.textContent=original;}}
function resultText(r){return `新增 ${r.inserted} · 更新 ${r.updated} · 未变 ${r.unchanged} · 退回 ${r.rejected}。详情见“更新记录”。`;}
document.querySelectorAll('.nav').forEach(button=>button.addEventListener('click',()=>switchView(button.dataset.view)));
for(const id of ['location','category','education','experience','source'])$(id).addEventListener('change',()=>{state.page=1;state.duplicatePage=1;reload();});
$('dataset').addEventListener('change',()=>{clear();reload(true);});
$('scope').addEventListener('change',()=>{clear();reload(true);});
let timer;$('q').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{state.page=1;state.duplicatePage=1;reload();},250);});
$('reset').addEventListener('click',()=>{clear();reload();});
$('currency').addEventListener('change',renderSalary);
$('overview').addEventListener('click',event=>{const button=event.target.closest('[data-field]');if(!button)return;if(button.dataset.field==='skill')state.skill=button.dataset.value;else $(button.dataset.field).value=button.dataset.value;state.page=1;state.duplicatePage=1;switchView('explore');reload();});
$('skill-filter').addEventListener('click',()=>{state.skill='';state.page=1;state.duplicatePage=1;reload();});
$('jobs').addEventListener('click',event=>{const button=event.target.closest('[data-job]');if(button)detail(button.dataset.job);});
$('close-detail').addEventListener('click',()=>$('detail').close());
$('prev').addEventListener('click',()=>{state.page--;reload();});$('next').addEventListener('click',()=>{state.page++;reload();});
$('sync').addEventListener('click',()=>action($('sync'),async()=>{notice('正在读取 Cloudflare 技术岗位来源，通常需要几秒钟…');const result=await api('/api/sync',{});notice(resultText(result));await load(true);if(state.view==='provenance')await loadRuns();}));
$('load-demo').addEventListener('click',()=>action($('load-demo'),async()=>{const result=await api('/api/demo',{});$('dataset').value='demo';clear();notice('已切换至模拟数据。'+resultText(result));await load(true);switchView('overview');}));
$('load-public-sample').addEventListener('click',()=>action($('load-public-sample'),async()=>{
  const result=await api('/api/public-sample',{});
  $('dataset').value='real';clear();switchView('overview');await load(true);
  notice(`已载入 ${result.total} 条真实岗位，来自 ${result.source_count} 个公开来源。采集于 ${date(result.captured_at)}；详情见“更新记录”。`,result.rejected>0);
}));
$('csv-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;if(file.size>2*1024*1024){notice('文件超过 2 MB',true);event.target.value='';return;}$('csv-content').value=await file.text();if(!$('import-source').value)$('import-source').value=file.name.replace(/\.csv$/i,'').replace(/[^\w\u4e00-\u9fff-]/g,'_').slice(0,60);});
$('import-form').addEventListener('submit',event=>{event.preventDefault();action($('import-submit'),async()=>{const demo=$('is-demo').checked;const result=await api('/api/import',{csv:$('csv-content').value,source:$('import-source').value,is_demo:demo});notice(resultText(result),result.rejected>0);$('dataset').value=demo?'demo':'real';clear();await load(true);});});
$('export').addEventListener('click',()=>{window.location.href='/api/export?'+filters();});
$('refresh-runs').addEventListener('click',()=>refreshView().catch(e=>notice(e.message,true)));
Object.assign(names,{
  management:['岗位管理','岗位管理','查看全部来源记录，人工下架岗位或解除下架状态。'],
  quality:['数据质量','数据质量','查看缺失与异常，核验学历、经验及疑似重复岗位。'],
  company:['企业岗位','企业岗位','管理本企业岗位，保存草稿、发布或停用。'],
  team:['成员授权','成员授权','添加本企业 HR 账号，管理岗位维护权限。'],
  favorites:['我的收藏','我的收藏','管理收藏与个人备注；已停用或撤回的岗位不再显示。'],
  resume:['我的简历','我的简历','上传并保存本人简历，需要时下载原文件。'],
});

function configureUser(user){
  state.user=user;
  const canManage=user.role==='admin'||user.role==='boss'||!!user.can_manage_jobs;
  state.canManage=canManage;
  for(const item of document.querySelectorAll('[data-roles]'))item.hidden=!item.dataset.roles.split(' ').includes(user.role);
  const roles={admin:'数据管理员',boss:'企业负责人',hr:'HR',candidate:'求职者'};
  const client=user.role==='candidate'?'普通端':user.role==='admin'?'数据管理':'企业端';
  $('account-name').textContent=user.display_name;
  $('account-role').textContent='身份：'+roles[user.role];
  $('client-name').textContent=user.org_name||'';
  document.title='recruitment-insights · '+client;
  $('sync').hidden=user.role!=='admin';$('load-demo').hidden=user.role!=='admin';$('load-public-sample').hidden=user.role!=='admin';
  $('scope-label').hidden=!user.org_id;
  $('import-ownership').textContent=user.org_id?'CSV 导入归属 '+user.org_name+'，公司字段以账号企业为准；新增记录为草稿，请在企业岗位中发布。':'管理员导入作为公开样本，载入模拟数据只供课程验收。';
  for(const input of document.querySelectorAll('#import-form input, #import-form textarea, #import-form button'))input.disabled=!canManage;
  $('new-company-job').hidden=!canManage;
}

async function startApp(){
  try{
    configureUser(await api('/api/auth/me'));
    if(state.user.org_id)$('scope').value='company';
    if(state.user.role==='candidate')switchView('explore');
    else if(state.user.org_id)switchView('company');
    await load(true);
  }
  catch(e){notice(e.message,true);}
}

async function refreshView(){
  if(state.view==='provenance'){await Promise.all([loadRuns(),loadAudit()]);}
  if(state.view==='quality')await load();
  if(state.view==='company')await loadCompany();
  if(state.view==='management')await loadManagement();
  if(state.view==='team')await loadTeam();
  if(state.view==='favorites')await loadFavorites();
  if(state.view==='resume')await loadResume();
}

const RESUME_MAX_BYTES=2097152;
function resumeNotice(message,error=false){
  $('resume-notice').textContent=message;$('resume-notice').className='notice'+(error?' error':'');$('resume-notice').hidden=!message;
}
function resumeFailure(error){
  return error.name==='TypeError'?'网络连接异常，请稍后重试。':error.name==='SyntaxError'?'服务暂时不可用，请稍后重试。':error.message||'简历操作失败，请稍后重试。';
}
function resumeSize(bytes){return `${(bytes/1024).toLocaleString('zh-CN',{maximumFractionDigits:1})} KiB（${bytes.toLocaleString('zh-CN')} 字节）`;}
function renderResume(){
  const saved=state.resume;
  $('resume-current').hidden=!saved;$('resume-empty').hidden=!!saved;
  if(!saved){$('resume-empty').textContent='你还没有上传简历。';return;}
  $('resume-filename').textContent=saved.filename;
  $('resume-type').textContent=saved.media_type==='application/pdf'?'PDF':saved.media_type==='application/vnd.openxmlformats-officedocument.wordprocessingml.document'?'DOCX':saved.media_type;
  $('resume-size').textContent=resumeSize(saved.size_bytes);
  $('resume-updated').textContent=date(saved.uploaded_at);
}
async function loadResume(){
  if(state.user?.role!=='candidate'||state.resumeBusy)return;
  const sequence=++state.resumeSequence,user=state.user;
  $('resume-refresh').disabled=true;resumeNotice('正在读取简历信息…');
  try{
    const data=await api('/api/resume');
    if(sequence!==state.resumeSequence||state.user!==user)return;
    state.resume=data.resume;renderResume();resumeNotice('');
  }catch(error){if(sequence===state.resumeSequence&&state.user===user)resumeNotice(resumeFailure(error),true);}
  finally{if(sequence===state.resumeSequence)$('resume-refresh').disabled=false;}
}
function resumeFileError(file){
  if(!file)return '请先选择要上传的 PDF 或 DOCX 简历。';
  if(!/\.(pdf|docx)$/i.test(file.name))return '仅支持 PDF 或 DOCX 文件。';
  if(!file.size||file.size>RESUME_MAX_BYTES)return '请选择非空文件，大小不能超过 2 MiB（2,097,152 字节）。';
  return '';
}
function resumeFileBase64(file){
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>{const result=reader.result;if(typeof result!=='string'||!result.includes(','))reject(new Error('文件读取失败，请重新选择后重试。'));else resolve(result.slice(result.indexOf(',')+1));};
    reader.onerror=reader.onabort=()=>reject(new Error('文件读取失败，请重新选择后重试。'));
    reader.readAsDataURL(file);
  });
}
async function uploadResume(){
  if(state.user?.role!=='candidate'||state.resumeBusy)return;
  const file=$('resume-file').files[0],problem=resumeFileError(file);
  if(problem){resumeNotice(problem,true);return;}
  state.resumeBusy=true;const sequence=++state.resumeSequence,user=state.user;
  $('resume-file').disabled=true;$('resume-refresh').disabled=true;
  await action($('resume-submit'),async()=>{
    try{
      resumeNotice('正在上传简历…');
      const content=await resumeFileBase64(file);
      const data=await api('/api/resume',{filename:file.name,content_base64:content});
      if(sequence!==state.resumeSequence||state.user!==user)return;
      state.resume=data.resume;renderResume();$('resume-file').value='';$('resume-selection').textContent='尚未选择文件。';resumeNotice('简历已保存。每个账号保留最新上传的一份。');
    }catch(error){if(sequence===state.resumeSequence&&state.user===user)resumeNotice(resumeFailure(error),true);}
    finally{state.resumeBusy=false;$('resume-file').disabled=false;$('resume-refresh').disabled=false;}
  });
}
$('resume-form').addEventListener('submit',event=>{event.preventDefault();uploadResume();});
$('resume-refresh').addEventListener('click',()=>loadResume());
$('resume-file').addEventListener('change',()=>{
  if(state.resumeBusy)return;
  const file=$('resume-file').files[0],problem=file?resumeFileError(file):'';
  $('resume-selection').textContent=file?`已选择：${file.name} · ${resumeSize(file.size)}`:'尚未选择文件。';
  resumeNotice(problem,!!problem);
});

function renderQuality(data){
  const metrics=[['检查岗位',data.total,'当前筛选结果'],['待关注岗位',data.warning_jobs,'有缺失或待核验字段'],['重复候选',data.duplicate_pending_pairs??0,`涉及 ${data.duplicate_pending_jobs??0} 条岗位`],['薪资样本',data.comparable_salary,'可比较的薪资记录']];
  $('quality-metrics').innerHTML=metrics.map(([label,value,note])=>`<article class="metric"><span class="metric-label">${label}</span><div class="metric-value">${value}</div><span class="metric-note">${note}</span></article>`).join('');
  const max=Math.max(1,...data.issues.map(i=>i.count));
  $('quality-chart').innerHTML=data.issues.length?data.issues.map(i=>`<div class="bar-row"><span>${escapeHtml(i.name)}</span><span class="bar-track"><span class="bar-fill" style="display:block;width:${i.count/max*100}%"></span></span><span class="bar-count">${i.count}</span></div>`).join(''):'<div class="empty">当前规则下未发现质量问题。</div>';
  $('quality-count').textContent=`共 ${data.warning_jobs} 条有提示的记录，显示前 ${data.shown} 条。可用上方筛选缩小范围；已核验记录也可从岗位探索进入详情重新查看。`;
  $('quality-rows').innerHTML=data.rows.map(j=>`<tr><td><strong>${escapeHtml(j.title)}</strong><small>${escapeHtml(j.company)} · ${escapeHtml(j.location)}</small></td><td>${j.issues.map(i=>escapeHtml(i.message)).join('<br>')}</td><td>${escapeHtml(j.source)}</td><td><button class="detail-button" data-job="${j.id}">查看与核验</button></td></tr>`).join('')||'<tr><td colspan="4">暂无待关注记录</td></tr>';
  renderDuplicates(data);
}

const duplicateLabels={pending:'待判断',same:'已确认同一岗位',different:'已确认不同岗位',stale:'来源变化 · 待复核',revoked:'判断已撤销'};
function renderDuplicates(data){
  state.duplicatePairs=data.duplicates||[];
  const pairs=state.duplicatePairs,total=data.duplicate_pairs??pairs.length;
  const page=data.duplicate_page||1,pages=data.duplicate_pages||1;
  state.duplicatePage=page;
  const counts=data.duplicate_status_counts||{};
  $('duplicate-count').textContent=`候选共 ${total} 对，${data.duplicate_pending_pairs??0} 对待处理（含待复核）；已确认同一岗位 ${counts.same??0} 对、不同岗位 ${counts.different??0} 对。本页 ${data.duplicates_shown??pairs.length} 对，待处理优先展示；历史判断可查看或撤销。`;
  $('duplicate-page-status').textContent=`第 ${page} / ${pages} 页`;
  $('duplicate-prev').disabled=page<=1;$('duplicate-next').disabled=page>=pages;
  $('duplicate-pairs').innerHTML=pairs.map((pair,index)=>{
    const decided=['same','different'].includes(pair.decision);
    const records=[pair.left,pair.right].map(j=>`<div class="duplicate-record"><strong>${escapeHtml(j.title)}</strong><p class="body-text">${escapeHtml(j.company)} · ${escapeHtml(j.location)}<br>来源：${escapeHtml(j.source)}</p><button type="button" class="detail-button" data-job="${j.id}">查看原文与详情</button></div>`).join('');
    return `<article class="duplicate-card"><div class="panel-head"><h3>候选 ${index+1}</h3><span class="badge ${escapeHtml(pair.decision)}">${escapeHtml(duplicateLabels[pair.decision]||pair.decision)}</span></div><div class="duplicate-records">${records}</div>
      <div id="duplicate-feedback-${index}" class="notice review-notice" role="status" aria-live="polite" hidden></div>
      ${pair.can_review?`<form class="review-form" data-duplicate-index="${index}"><label>判断结果<select id="duplicate-decision-${index}" required><option value=""${decided?'':' selected'}>请选择</option>${selectOptions(['same','different'],decided?pair.decision:'').replace('>same<','>同一招聘岗位<').replace('>different<','>不同招聘岗位<')}</select></label><label>判断依据<textarea id="duplicate-reason-${index}" rows="2" maxlength="1000" required placeholder="例如：两条来源指向同一原始职位；或实际属于不同城市、不同招聘批次">${escapeHtml(pair.reason||'')}</textarea></label><div class="card-actions"><button class="primary" type="submit">保存判断</button>${['same','different','stale'].includes(pair.decision)?`<button class="secondary" type="button" data-revoke-duplicate="${index}">撤销判断</button>`:''}</div></form>`:`<p class="body-text">${pair.reason?'判断依据：'+escapeHtml(pair.reason):pair.can_view_reason===false?'判断依据仅对相应企业或管理员可见。':'当前账号仅可查看此候选。'}</p>`}</article>`;
  }).join('')||'<div class="empty">当前筛选没有相似记录候选。</div>';
}
async function saveDuplicate(index,revoke=false){
  const pair=state.duplicatePairs?.[index];
  if(!pair?.can_review)return;
  const form=$('duplicate-pairs').querySelector(`[data-duplicate-index="${index}"]`),feedback=$('duplicate-feedback-'+index);
  if(form.dataset.busy)return;
  form.dataset.busy='true';for(const button of form.querySelectorAll('button'))button.disabled=true;
  const reason=$('duplicate-reason-'+index).value.trim();
  try{
    await api('/api/duplicates',{left_id:pair.left.id,right_id:pair.right.id,left_hash:pair.left_hash,right_hash:pair.right_hash,version:pair.version,decision:revoke?'revoke':$('duplicate-decision-'+index).value,reason:reason||(revoke?'撤销此前判断':'')});
    notice(revoke?'重复判断已撤销，来源记录保留。':'重复判断已保存，来源记录保留。');
    await load();
  }catch(error){
    feedback.className='notice review-notice error';feedback.hidden=false;
    feedback.innerHTML=escapeHtml(error.status===409?'记录或判断已更新，请重新检查候选后再提交。':error.message)+(error.status===409?'<br><button type="button" class="secondary" data-reload-duplicates>重新检查候选</button>':'');
  }finally{delete form.dataset.busy;for(const button of form.querySelectorAll('button'))button.disabled=false;}
}

async function loadAudit(){
  const events=await api('/api/audit');
  const actions={grant_hr:'调整 HR 权限',create_hr:'创建 HR 账号',manage_job:'保存岗位',deactivate_job:'人工下架岗位',restore_job:'恢复岗位',review_confirm:'确认字段核验',review_revoke:'撤销字段核验',duplicate_same:'确认同一岗位',duplicate_different:'确认不同岗位',duplicate_revoke:'撤销重复判断'};
  $('audit-events').innerHTML=events.map(e=>`<div class="run"><div class="run-header"><span>${escapeHtml(e.display_name)} · ${actions[e.action]||escapeHtml(e.action)} · #${escapeHtml(e.target)}</span><small>${date(e.created*1000)}</small></div></div>`).join('')||'<p class="body-text">暂无关键操作记录。</p>';
}

function lifecycleStatus(row,company=false){
  const label=row.manual_inactive?'人工下架':row.source_active===false?'来源已结束':!row.active?'已停用':company?(row.published?'已发布':'草稿'):'启用';
  return `<span class="badge ${row.active&&(company?row.published:true)?'published':''}">${label}</span>${row.source_active===false&&row.manual_inactive?'<small class="lifecycle-status-note">来源已结束，解除人工下架仍不公开</small>':''}`;
}
function lifecycleActionButton(row){
  const action=row.manual_inactive?'restore':'deactivate';
  return `<button class="detail-button" type="button" data-lifecycle-job="${row.id}" data-lifecycle-action="${action}" aria-label="${action==='restore'?'恢复':'下架'} ${escapeHtml(row.title)}">${action==='restore'?'恢复':'下架'}</button>`;
}
async function loadManagement(){
  const sequence=++state.managementSequence;
  const params=new URLSearchParams({dataset:$('management-dataset').value,q:$('management-q').value.trim(),status:$('management-status').value,page:String(state.managementPage)});
  const data=await api('/api/admin/jobs?'+params);
  if(sequence!==state.managementSequence)return;
  const pages=Math.max(1,Math.ceil(data.total/15));
  if(state.managementPage>pages){state.managementPage=pages;return loadManagement();}
  state.managementPage=data.page;state.managementJobs=data.items;
  $('management-count').textContent=`${data.total} 条`;
  $('management-rows').innerHTML=data.items.map(j=>`<tr><td><strong>${escapeHtml(j.title)}</strong><small>${escapeHtml(j.company)}</small></td><td>${escapeHtml(j.location)}</td><td>${escapeHtml(j.source)}<small>${j.is_demo?'模拟数据':'真实 / 自有数据'}</small></td><td>${lifecycleStatus(j)}</td><td><div class="lifecycle-record">${j.lifecycle_reason?'下架原因：'+escapeHtml(j.lifecycle_reason):'暂无人工下架原因'}</div>${j.lifecycle_updated_at?`<small>最近状态变更：${escapeHtml(date(j.lifecycle_updated_at))}</small>`:''}</td><td><div class="row-actions"><button class="detail-button" type="button" data-job="${j.id}">查看</button>${lifecycleActionButton(j)}</div></td></tr>`).join('')||'<tr><td colspan="6"><div class="empty">没有匹配的岗位，请调整管理筛选条件。</div></td></tr>';
  $('management-page-status').textContent=`第 ${data.page} / ${pages} 页 · 共 ${data.total} 条`;
  $('management-prev').disabled=data.page<=1;$('management-next').disabled=data.page>=pages;
}
function lifecycleNotice(message,error=false,reloadRequired=false){
  const element=$('lifecycle-notice');element.hidden=false;element.className='notice'+(error?' error':'');
  element.innerHTML=escapeHtml(message)+(reloadRequired?'<br><button class="secondary" type="button" data-reload-lifecycle>重新载入最新状态（保留原因）</button>':'');
}
function renderLifecycleTarget(){
  const row=state.lifecycleRow,restore=state.lifecycleAction==='restore';
  $('lifecycle-heading').textContent=restore?'恢复岗位':'岗位下架';
  $('lifecycle-title').textContent=row.title;
  $('lifecycle-context').textContent=[row.company,row.location,row.source].filter(Boolean).join(' · ');
  $('lifecycle-explanation').textContent=restore?(row.source_active===false?'该岗位的来源已结束。此操作仅解除人工下架，岗位仍停用，不会重新公开。':row.published===false?'此操作解除人工下架。该岗位仍是企业草稿，恢复不会自动发布。':'此操作解除人工下架，公开可见性仍由来源与发布状态决定。'):row.source_active===false?'该岗位的来源已结束，当前已停用。此操作会设置人工下架；即使来源恢复，也须先解除人工下架才可能公开。':'确认下架此岗位后，它将退出公开岗位列表。岗位内容与历史记录会保留，可在之后恢复。';
  $('lifecycle-reason-label').hidden=restore;$('lifecycle-reason').required=!restore;$('lifecycle-reason').disabled=restore;
  $('lifecycle-help').textContent=restore?'恢复操作会记录在更新记录中；恢复不会更改发布状态。':'下架原因与状态变更会保存，可在岗位管理与更新记录中查看。岗位记录会保留，后续可恢复。';
  $('lifecycle-submit').textContent=restore?'确认恢复':'确认下架';
}
function openLifecycle(row,action){
  if(state.lifecycleBusy||!state.canManage)return;
  state.lifecycleRow=row;state.lifecycleAction=action;state.lifecycleConflict=false;
  $('lifecycle-reason').value='';$('lifecycle-notice').hidden=true;$('lifecycle-submit').disabled=false;
  renderLifecycleTarget();
  const dialog=$('lifecycle-dialog');
  if(!dialog.open&&typeof dialog.showModal==='function')dialog.showModal();
  (action==='deactivate'?$('lifecycle-reason'):$('lifecycle-submit')).focus();
}
function closeLifecycle(){
  if(state.lifecycleBusy)return;
  const dialog=$('lifecycle-dialog');if(typeof dialog.close==='function')dialog.close();
  state.lifecycleRow=null;
}
async function reloadLifecycle(){
  const id=state.lifecycleRow?.id;if(!id||state.lifecycleBusy)return;
  const button=$('lifecycle-notice').querySelector('[data-reload-lifecycle]');if(button)button.disabled=true;
  try{
    const row=await api('/api/jobs/'+id);
    if(state.lifecycleRow?.id!==id)return;
    if(row.edit_token==null){lifecycleNotice('当前账号已失去此岗位的维护权限。请关闭此面板并刷新岗位列表。',true);return;}
    state.lifecycleRow={...row,title:row.payload?.title??row.title,company:row.payload?.company??row.company,location:row.payload?.location??row.location};
    renderLifecycleTarget();
    const available=state.lifecycleAction==='restore'?row.manual_inactive:!row.manual_inactive;
    state.lifecycleConflict=!available;$('lifecycle-submit').disabled=!available;
    lifecycleNotice(available?'已载入最新状态，下架原因仍保留。请核对岗位与状态后再次确认。':'岗位状态已变化，当前操作已无需执行。请关闭面板并刷新岗位列表。',!available);
  }catch(error){lifecycleNotice(error.status===403?'当前账号没有此岗位的维护权限，请刷新岗位列表后检查账号权限。':error.message,true,true);}
  finally{if(button)button.disabled=false;}
}
async function saveLifecycle(){
  const row=state.lifecycleRow;if(!row||state.lifecycleBusy||state.lifecycleConflict)return;
  const action=state.lifecycleAction,reason=$('lifecycle-reason').value.trim();
  if(action==='deactivate'&&(!reason||reason.length>500)){lifecycleNotice('请填写 1–500 字的下架原因。',true);$('lifecycle-reason').focus();return;}
  if(row.edit_token==null){lifecycleNotice('缺少岗位最新状态，请重新载入后再确认。',true,true);state.lifecycleConflict=true;$('lifecycle-submit').disabled=true;return;}
  state.lifecycleBusy=true;
  for(const id of ['lifecycle-submit','close-lifecycle','cancel-lifecycle','lifecycle-reason'])$(id).disabled=true;
  let result;
  try{
    result=await api('/api/jobs/lifecycle',{id:Number(row.id),action,...(action==='deactivate'?{reason}:{}),expected_edit_token:row.edit_token});
  }catch(error){
    if(error.status===409){state.lifecycleConflict=true;lifecycleNotice('岗位已被其他操作更新。你填写的下架原因仍保留；请重新载入最新状态后确认。',true,true);}
    else lifecycleNotice(error.status===403?'当前账号没有此岗位的维护权限，权限可能已被收回。请关闭面板并刷新岗位列表。':error.message,true);
  }finally{
    state.lifecycleBusy=false;
    for(const id of ['close-lifecycle','cancel-lifecycle'])$(id).disabled=false;
    $('lifecycle-submit').disabled=!!state.lifecycleConflict;$('lifecycle-reason').disabled=action==='restore';
  }
  if(!result)return;
  closeLifecycle();
  const title='“'+row.title+'”';
  const message=action==='deactivate'?`${title}已人工下架，岗位记录保留，可在之后恢复。`:result.source_active===false||!result.active?`${title}已解除人工下架，但来源已结束，岗位仍停用，不会公开。`:(result.published??row.published)===false?`${title}已恢复启用，仍保留为企业草稿。`:`${title}已解除人工下架，公开可见性按来源与发布状态确定。`;
  notice(message+' 操作已记入更新记录。');
  try{
    await Promise.all([load(true),state.user.role==='admin'?loadManagement():loadCompany(),loadAudit()]);
    if(state.detail?.id===row.id){const updated=await api('/api/jobs/'+row.id);renderDetail(updated);}
  }catch(error){notice(message+' 读取最新数据失败：'+error.message,true);}
}

async function loadCompany(){
  configureUser(await api('/api/auth/me'));
  state.companyJobs=await api('/api/company/jobs');
  $('company-rows').innerHTML=state.companyJobs.map(j=>`<tr><td><strong>${escapeHtml(j.title)}</strong></td><td>${escapeHtml(j.location)}</td><td>${lifecycleStatus(j,true)}${j.lifecycle_reason?`<small class="lifecycle-record">下架原因：${escapeHtml(j.lifecycle_reason)}</small>`:''}</td><td>${j.is_demo?'模拟数据':'自有数据'}</td><td><div class="row-actions"><button class="detail-button" data-job="${j.id}">查看</button>${state.canManage?`<button class="detail-button" data-edit-job="${j.id}">编辑</button>${lifecycleActionButton(j)}`:''}</div></td></tr>`).join('')||'<tr><td colspan="5"><div class="empty">本企业暂无岗位。</div></td></tr>';
  if(!state.canManage)closeCompanyEditor();
}

function closeCompanyEditor(){state.companyEditorSequence++;$('company-editor').hidden=true;}
async function editCompanyJob(id){
  const sequence=++state.companyEditorSequence;
  let row;
  try{row=id?await api('/api/jobs/'+Number(id)):null;}
  catch(error){if(sequence!==state.companyEditorSequence)return;throw error;}
  if(sequence!==state.companyEditorSequence)return;
  const raw=row?.raw;
  state.companyEditToken=row?.edit_token??null;
  $('company-form').reset();$('company-job-id').value=row?.id||'';
  $('company-editor-title').textContent=row?'编辑岗位':'新增岗位';
  $('company-edit-notice').hidden=true;
  const fields={title:'title',location:'location','salary-text':'salary_text',currency:'currency',period:'salary_period',education:'education',experience:'experience',url:'source_url',description:'description'};
  for(const [field,key] of Object.entries(fields))$('company-'+field).value=raw?.[key]||({currency:'CNY',period:'month'}[field]||'');
  $('company-salary-min').value=raw?.salary_min??'';
  $('company-salary-max').value=raw?.salary_max??'';
  $('company-demo').checked=row?.is_demo??true;$('company-published').checked=row?.published??false;$('company-active').checked=!row?.manual_inactive;
  $('company-editor').hidden=false;$('company-title').focus();$('company-editor').scrollIntoView({block:'start',behavior:'smooth'});
}

async function loadTeam(){
  const members=await api('/api/team');
  $('team-members').innerHTML=members.map(m=>`<div class="member-row"><div><strong>${escapeHtml(m.display_name)}</strong><p class="body-text">${escapeHtml(m.username)} · ${m.can_manage_jobs?'可维护岗位':'只读'}</p></div><button class="secondary" data-member="${m.id}" data-allow="${!m.can_manage_jobs}">${m.can_manage_jobs?'收回维护权':'授予维护权'}</button></div>`).join('')||'<p class="body-text">尚未添加 HR。</p>';
}

async function loadFavorites(){
  const jobs=await api('/api/favorites');
  $('favorite-cards').innerHTML=jobs.map(j=>`<article class="panel"><span class="badge">${j.is_demo?'模拟岗位':'已收藏'}</span><h2>${escapeHtml(j.title)}</h2><p class="body-text">${escapeHtml(j.company)} · ${escapeHtml(j.location)}<br>${escapeHtml(salaryLabel(j))}</p><label class="body-text" for="note-${j.id}">个人备注</label><textarea id="note-${j.id}" rows="3" maxlength="3000">${escapeHtml(j.note)}</textarea><div class="card-actions"><button class="secondary" data-note="${j.id}">保存备注</button><button class="detail-button" data-job="${j.id}">查看岗位</button><button class="quiet" data-remove-favorite="${j.id}">取消收藏</button></div></article>`).join('')||'<div class="empty">暂无可见的收藏岗位。前往岗位探索，在详情中收藏。</div>';
}

$('logout').addEventListener('click',()=>action($('logout'),async()=>{await api('/api/auth/logout',{});window.location.assign('/login');}));
function reloadManagement(){return loadManagement().catch(e=>notice(e.status===403?'当前账号没有岗位管理权限，请检查登录身份。':'读取岗位管理数据失败：'+e.message,true));}
for(const id of ['management-dataset','management-status'])$(id).addEventListener('change',()=>{state.managementPage=1;reloadManagement();});
let managementTimer;
$('management-q').addEventListener('input',()=>{clearTimeout(managementTimer);state.managementSequence++;managementTimer=setTimeout(()=>{state.managementPage=1;reloadManagement();},250);});
$('management-reset').addEventListener('click',()=>{clearTimeout(managementTimer);$('management-q').value='';$('management-status').value='all';state.managementPage=1;reloadManagement();});
$('refresh-management').addEventListener('click',()=>reloadManagement());
$('management-prev').addEventListener('click',()=>{state.managementPage=Math.max(1,state.managementPage-1);reloadManagement();});
$('management-next').addEventListener('click',()=>{state.managementPage++;reloadManagement();});
$('management-rows').addEventListener('click',event=>{const button=event.target.closest('[data-job]');if(button)detail(button.dataset.job);});
for(const id of ['management-rows','company-rows'])$(id).addEventListener('click',event=>{
  const button=event.target.closest('[data-lifecycle-job]');if(!button)return;
  const rows=id==='management-rows'?state.managementJobs:state.companyJobs;
  const row=rows?.find(j=>Number(j.id)===Number(button.dataset.lifecycleJob));if(row)openLifecycle(row,button.dataset.lifecycleAction);
});
$('lifecycle-form').addEventListener('submit',event=>{event.preventDefault();saveLifecycle();});
for(const id of ['close-lifecycle','cancel-lifecycle'])$(id).addEventListener('click',closeLifecycle);
$('lifecycle-dialog').addEventListener('cancel',event=>{if(state.lifecycleBusy)event.preventDefault();else state.lifecycleRow=null;});
$('lifecycle-notice').addEventListener('click',event=>{if(event.target.closest('[data-reload-lifecycle]'))reloadLifecycle();});
$('refresh-quality').addEventListener('click',()=>reload());
$('duplicate-prev').addEventListener('click',()=>{state.duplicatePage=Math.max(1,state.duplicatePage-1);reload();});
$('duplicate-next').addEventListener('click',()=>{state.duplicatePage++;reload();});
$('new-company-job').addEventListener('click',()=>editCompanyJob().catch(e=>notice(e.message,true)));
$('cancel-company-edit').addEventListener('click',closeCompanyEditor);
$('company-edit-notice').addEventListener('click',event=>{const button=event.target.closest('[data-reload-company]');if(button)action(button,()=>editCompanyJob($('company-job-id').value));});
for(const id of ['company-rows','quality-rows','favorite-cards','duplicate-pairs'])$(id).addEventListener('click',event=>{const button=event.target.closest('[data-job]');if(button)detail(button.dataset.job);});
$('company-rows').addEventListener('click',event=>{const button=event.target.closest('[data-edit-job]');if(button)editCompanyJob(button.dataset.editJob).catch(e=>notice(e.message,true));});
$('company-form').addEventListener('submit',event=>{event.preventDefault();action($('company-submit'),async()=>{
  const record={};for(const [field,key] of Object.entries({title:'title',location:'location','salary-text':'salary_text',currency:'currency',period:'salary_period','salary-min':'salary_min','salary-max':'salary_max',education:'education',experience:'experience',url:'source_url',description:'description'}))record[key]=$('company-'+field).value;
  const id=$('company-job-id').value?Number($('company-job-id').value):null;
  let result;
  try{
    result=await api('/api/company/jobs',{id,record,is_demo:$('company-demo').checked,published:$('company-published').checked,active:$('company-active').checked,...(id?{expected_edit_token:state.companyEditToken}:{})});
  }catch(error){
    const feedback=$('company-edit-notice');
    feedback.className='notice error';feedback.hidden=false;
    feedback.innerHTML=error.status===409?'岗位已被其他操作更新，你填写的内容仍保留在下方。请先复制需要保留的修改，再重新载入最新版本。重新载入会替换当前表单。<br><button class="secondary" type="button" data-reload-company>重新载入最新版本</button>':escapeHtml(error.message);
    feedback.scrollIntoView({block:'nearest'});
    return;
  }
  $('company-editor').hidden=true;notice(`岗位 #${result.id} 已保存，${!result.active?'已停用':result.published?'已发布到普通端':'保留为企业草稿'}。`);await loadCompany();await load(true);
});});
$('team-members').addEventListener('click',event=>{const button=event.target.closest('[data-member]');if(button)action(button,async()=>{await api('/api/team',{id:Number(button.dataset.member),can_manage:button.dataset.allow==='true'});notice('授权已更新，对下一次操作立即生效。');await loadTeam();});});
$('team-form').addEventListener('submit',event=>{event.preventDefault();action($('team-submit'),async()=>{await api('/api/team',{username:$('team-username').value,display_name:$('team-name').value,password:$('team-password').value,can_manage:$('team-manage').checked});$('team-form').reset();notice('HR 账号已创建。');await loadTeam();});});
$('detail-content').addEventListener('click',event=>{const button=event.target.closest('[data-save-favorite]');if(button)action(button,async()=>{await api('/api/favorites',{job_id:Number(button.dataset.saveFavorite)});button.dataset.saved='true';$('detail').close();notice('已收藏，可在“我的收藏”填写个人备注。');if(state.view==='favorites')await loadFavorites();});});
$('detail-content').addEventListener('submit',event=>{
  const form=event.target.closest('[data-review-field]');if(!form)return;
  event.preventDefault();saveReview(form.dataset.reviewField,'confirm');
});
$('detail-content').addEventListener('change',event=>{
  const input=event.target,match=/^review-(education|experience)-value$/.exec(input.id);
  if(!match)return;
  const field=match[1],unknown=/未知|未披露/.test(input.value),condition=$('review-'+field+'-condition');
  if(unknown&&state.reviewOptions.conditions.includes('未发现明确要求'))condition.value='未发现明确要求';
  else if(condition.value==='未发现明确要求')condition.value='';
  if(field==='education')$('review-education-alternatives').hidden=input.value!=='多种学历选择';
  if(field==='experience')$('review-experience-range').hidden=input.value!=='年限范围';
});
$('detail-content').addEventListener('click',event=>{
  const revoke=event.target.closest('[data-revoke-field]');if(revoke)saveReview(revoke.dataset.revokeField,'revoke');
  if(event.target.closest('[data-reload-detail]')&&state.detail)detail(state.detail.id);
});
$('duplicate-pairs').addEventListener('submit',event=>{
  const form=event.target.closest('[data-duplicate-index]');if(!form)return;
  event.preventDefault();saveDuplicate(Number(form.dataset.duplicateIndex));
});
$('duplicate-pairs').addEventListener('click',event=>{
  const revoke=event.target.closest('[data-revoke-duplicate]');if(revoke)saveDuplicate(Number(revoke.dataset.revokeDuplicate),true);
  const reloadButton=event.target.closest('[data-reload-duplicates]');if(reloadButton)action(reloadButton,()=>load());
});
$('favorite-cards').addEventListener('click',event=>{const button=event.target.closest('[data-note], [data-remove-favorite]');if(!button)return;action(button,async()=>{const id=Number(button.dataset.note||button.dataset.removeFavorite);await api('/api/favorites',{job_id:id,...(button.dataset.note?{note:$('note-'+id).value}:{remove:true})});notice(button.dataset.note?'个人备注已保存。':'已取消收藏。');if(!button.dataset.note)await loadFavorites();});});

startApp();
// Poll stored data, never the external source; hidden tabs do not poll.
setInterval(()=>{if(!document.hidden&&['overview','explore'].includes(state.view))reload();},30000);
