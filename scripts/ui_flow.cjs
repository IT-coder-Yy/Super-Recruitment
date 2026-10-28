/* 只对 serve_test_demo.py 创建的隔离库运行：真实表单闭环、两轮面试和重启核对。 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.env.TALENTFLOW_URL;
assert.match(base || '', /^http:\/\/127\.0\.0\.1:801[2-9]$/, '必须显式选择隔离验收服务');
const output = path.resolve(process.env.UI_SCREENSHOT_DIR || '.test-runs/ui-flow');
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:process.env.UI_BROWSER || 'msedge'});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*',route=>new URL(route.request().url()).origin===base ? route.continue():route.abort());
  const candidateContext=await browser.newContext({viewport:{width:1440,height:1000}});
  await candidateContext.route('**/*',route=>new URL(route.request().url()).origin===base ? route.continue():route.abort());
  const page=await context.newPage(); const errors=[], steps=[];
  page.on('pageerror',e=>errors.push(e.message)); page.on('dialog',d=>d.accept());
  async function shot(name,target=page){await target.evaluate(()=>window.scrollTo({top:0,left:0,behavior:'instant'}));await target.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
  async function submit(form){
    const action=await form.getAttribute('action');
    assert.equal(await form.evaluate(f=>f.checkValidity()),true,await form.evaluate(f=>Array.from(f.elements).filter(e=>e.validity && !e.validity.valid).map(e=>e.name+': '+e.validationMessage).join(';')));
    const response=page.waitForResponse(r=>r.url()===base+action && r.request().method()==='POST').then(r=>r.json());
    const [result]=await Promise.all([response,page.waitForNavigation({waitUntil:'domcontentloaded'}),form.locator('button[type=submit]').first().click()]);
    assert.equal(result.ok,true,JSON.stringify(result)); return result;
  }
  try{
    await page.goto(base+'/login');
    await page.getByLabel('用户名',{exact:true}).fill('demo-admin');
    await page.getByLabel('密码',{exact:true}).fill('demo123456');
    await Promise.all([page.waitForURL(base+'/'),page.getByRole('button',{name:'登录',exact:true}).click()]);
    // 哨兵：服务必须呈现隔离 Mock 配置，避免把本脚本用于真实招聘数据。
    await page.goto(base+'/settings');
    assert.equal(await page.locator('[name="provider"]').inputValue(),'mock');
    assert.ok((await page.locator('#mail .status').textContent()).includes('Mock'));
    if(process.env.UI_BATCH_ONLY==='1'){
      await page.goto(base+'/import');
      const form=page.locator('#resumeUploadForm');
      const jobId=await form.locator('[name=job_id] option').nth(1).getAttribute('value');
      await form.locator('[name=job_id]').selectOption(jobId);
      await page.locator('#resumeFiles').setInputFiles([
        {name:'batch-good.txt',mimeType:'text/plain',buffer:Buffer.from('姓名：虚构批量\n手机号：13900009998\n邮箱：batch@example.test\n项目：Python API')},
        {name:'batch-broken.pdf',mimeType:'application/pdf',buffer:Buffer.from('broken pdf')},
        {name:'batch-empty.txt',mimeType:'text/plain',buffer:Buffer.alloc(0)}
      ]);
      const response=page.waitForResponse(r=>r.url()===base+'/api/import/files'&&r.request().method()==='POST').then(r=>r.json());
      const [uploaded]=await Promise.all([response,page.waitForURL(/\/import\?task_id=/),form.locator('button[type=submit]').click()]);
      assert.equal(uploaded.tasks.length,2); assert.equal(uploaded.errors.length,1);
      const csrf=await page.locator('meta[name=csrf-token]').getAttribute('content');
      const states=[];
      for(const task of uploaded.tasks){
        const parsed=await(await context.request.post(base+`/api/import/tasks/${task.id}/parse`,{headers:{'X-CSRF-Token':csrf}})).json();
        states.push(parsed.task.status);
        await page.goto(base+`/import?task_id=${task.id}`); await shot('batch-'+parsed.task.status);
      }
      assert.deepEqual(states,['needs_review','failed']); assert.deepEqual(errors,[]);
      await fs.writeFile(path.join(output,'batch-results.json'),JSON.stringify({status:'passed',states,upload_errors:uploaded.errors,errors},null,2));
      return;
    }
    if(process.env.UI_VERIFY_RESTART==='1'){
      assert.equal(await page.locator('[name=model]').inputValue(),'synthetic-persisted-model');
      assert.equal(await page.locator('[name=base_url]').inputValue(),'https://model.example.test/v1');
      const previous=JSON.parse(await fs.readFile(path.join(output,'flow-results.json'),'utf8'));
      const record=await(await context.request.get(base+'/api/candidates/'+previous.candidate_id)).json();
      assert.equal(record.applications[0].status,previous.final_state);
      await shot('08-settings-after-restart');
      await page.goto(base+'/onboarding'); await shot('09-business-after-restart');
      assert.deepEqual(errors,[]);
      await fs.writeFile(path.join(output,'restart-results.json'),JSON.stringify({status:'passed',settings:'persisted',application_id:previous.application_id,state:previous.final_state,errors},null,2));
      return;
    }
    await page.locator('[name=model]').fill('synthetic-persisted-model');
    await page.locator('[name=base_url]').fill('https://model.example.test/v1');
    await Promise.all([page.waitForURL(base+'/settings?saved=1'),page.getByRole('button',{name:'保存模型配置',exact:true}).click()]);
    await shot('00-settings-saved'); steps.push('设置页保存合成 Mock 配置');
    await page.goto(base+'/import');
    const jobId=await page.locator('#resumeUploadForm select[name=job_id] option').nth(1).getAttribute('value');
    await page.locator('#resumeUploadForm select[name=job_id]').selectOption(jobId);
    const name='虚构验收'+Date.now().toString().slice(-6);
    await page.locator('#resumeFiles').setInputFiles({name:'ui-flow.txt',mimeType:'text/plain',buffer:Buffer.from(`姓名：${name}\n手机号：13900009999\n技能：Python FastAPI SQL pytest\n项目：独立实现订单 API 和事务回滚测试。`)});
    await Promise.all([page.waitForURL(/\/import\?task_id=/),page.locator('#resumeUploadForm button[type=submit]').click()]);
    const taskId=new URL(page.url()).searchParams.get('task_id');
    await page.locator(`[data-import-action=parse][data-task-id="${taskId}"]`).click();
    await page.locator('#previewEdit').waitFor();
    await page.locator('#previewEdit [name=email]').fill('ui-flow@example.test');
    const supplement=page.waitForResponse(r=>r.url().endsWith(`/tasks/${taskId}/supplement`));
    await page.getByRole('button',{name:'保存补充资料',exact:true}).click();
    assert.equal((await supplement).status(),200);
    await page.waitForFunction(()=>document.querySelector('#previewEdit [name=email]')?.value==='ui-flow@example.test');
    await shot('01-preview-corrected');
    await Promise.all([page.waitForURL(/\/candidates\/\d+/),page.locator('[data-import-action="confirm-evaluate"]').click()]);
    const candidateUrl=new URL(page.url()).pathname;
    const candidate=await (await context.request.get(base+'/api'+candidateUrl)).json();
    const app=candidate.applications[0]; const appId=app.id;
    async function state(expected){const value=await(await context.request.get(base+'/api'+candidateUrl)).json();assert.equal(value.applications[0].status,expected);}
    assert.equal(app.status,'evaluation_review');
    steps.push('上传、程序解析、人工补邮箱、确认入库、显式 Mock 评估');
    await submit(page.locator(`form[action="/api/applications/${appId}/decision"]`).filter({has:page.locator('[value=assessment_pending]')}));
    for(const [kind,next] of [['assessment','written_pending'],['written','interview_pending']]){
      await page.goto(base+`/exams?application_id=${appId}`);
      const create=page.locator('form[action="/api/exams/resources"]');
      await create.locator('[name=job_id]').selectOption(jobId); await create.locator('[name=kind]').selectOption(kind);
      await create.locator('[name=title]').fill('虚构'+kind+'试卷'); await create.locator('[name=url]').fill(`https://exam.example.test/${kind}`);
      const resource=await submit(create);
      await page.goto(base+`/exams?application_id=${appId}&resource_id=${resource.resource_id}`);
      await shot('02-'+kind+'-preview');
      await submit(page.locator(`form[action="/api/applications/${appId}/exams/send"]`));
      await state(kind==='assessment'?'assessment_pending':'written_pending');
      const decide=page.locator(`form[action="/api/applications/${appId}/exams/decision"]`);
      await decide.locator('[name=note]').fill('虚构结果：HR 已从公司考试平台核实通过');
      await submit(decide); await state(next);
      steps.push(kind+' 预览、Mock 邮件、发放不推进、HR 通过');
    }
    const rounds=[];
    for(const action of ['next_round','offer']){
    await submit(page.locator(`form[action="/api/applications/${appId}/schedule"]`).first());
    const current=await(await context.request.get(base+'/api'+candidateUrl)).json(); const interviewId=current.applications[0].interview_id;
    rounds.push(interviewId);
    await page.goto(base+`/interviews#interview-${interviewId}`);
    const booking=await page.locator(`#booking-${interviewId}`).inputValue();
    await submit(page.locator(`form[action="/api/interviews/${interviewId}/send-invite"]`));
    const publicPage=await candidateContext.newPage(); publicPage.on('dialog',d=>d.accept());
    await publicPage.goto(booking);
    await Promise.all([publicPage.waitForNavigation(),publicPage.locator('.public-slot button[type=submit]').first().click()]);
    await state('interview_confirmed'); await publicPage.close();
    await page.reload();
    await page.goto(base+`/interviews#interview-${interviewId}`);
    const feedback=page.locator(`form[action="/api/interviews/${interviewId}/feedback"]`);
    await feedback.locator('[name=next_action]').selectOption(action);
    await feedback.locator('[name=comment]').fill('虚构验收反馈：实现和测试证据已核实');
    await shot('round-'+rounds.length+'-feedback');
    await submit(feedback); await state(action==='next_round'?'next_round':'offer_pending');
    await page.goto(base+'/interviews');
    }
    assert.notEqual(rounds[0],rounds[1]);
    assert.ok((await page.locator('body').textContent()).includes('虚构验收反馈'));
    steps.push('两轮独立排期、候选人公开确认、第一轮下一轮、第二轮 Offer，历史反馈保留');
    await page.goto(base+'/offers');
    await page.getByRole('link',{name,exact:true}).click();
    const terms=page.locator('form[action$="/terms"]');
    for(const [key,value] of Object.entries({company_name:'虚构验收公司',job_title:'虚构后端实习生',office_location:'虚构办公区',report_location:'虚构前台',salary:'虚构：300 元/天',subsidy:'无',report_time:new Date(Date.now()+7*86400000).toISOString().slice(0,10),expires_at:new Date(Date.now()+3*86400000).toISOString().slice(0,16)})) await terms.locator(`[name=${key}]`).fill(value);
    await shot('03-offer-terms'); await submit(terms);
    const offerId=(await terms.getAttribute('action')).split('/')[3];
    const returned=page.locator(`form[action="/api/offers/${offerId}/return"]`);
    await returned.locator('[name=reason]').fill('虚构审批意见：调整薪资后重审'); await submit(returned);
    await terms.locator('[name=salary]').fill('虚构：350 元/天'); await submit(terms);
    await submit(page.locator(`form[action="/api/offers/${offerId}/approve"]`).first());
    await submit(page.locator(`form[action="/api/offers/${offerId}/send"]`).first()); await state('offer_sent');
    const offerUrl=await page.locator('tr').filter({hasText:name}).getByRole('link',{name:'候选人链接',exact:true}).getAttribute('href');
    const offerPage=await candidateContext.newPage(); offerPage.on('dialog',d=>d.accept());
    await offerPage.goto(base+offerUrl);
    assert.ok((await offerPage.locator('h1').textContent()).includes('虚构验收公司'));
    await shot('04-public-offer',offerPage);
    await Promise.all([offerPage.waitForNavigation(),offerPage.getByRole('button',{name:'接受 Offer',exact:true}).click()]);
    await offerPage.close();
    await state('onboarding'); steps.push('条款编辑、退回修改、重审、Mock 发放、候选人接受');
    await page.goto(base+'/onboarding');
    const registration=page.locator(`form[action="/api/applications/${appId}/onboarding"]`);
    await registration.locator('[name=expected_date]').fill(new Date().toISOString().slice(0,10));
    await registration.locator('[name=location]').fill('虚构前台'); await registration.locator('[name=note]').fill('虚构入职登记');
    await submit(registration);
    for(const action of ['needs_supplement','update','defer','update']){
      await registration.locator('[name=action]').selectOption(action);
      await registration.locator('[name=expected_date]').fill(new Date(Date.now()+(action==='defer'?10:0)*86400000).toISOString().slice(0,10));
      await registration.locator('[name=note]').fill('虚构办理：'+action);
      await submit(registration); await state('onboarding');
    }
    steps.push('需补充后补齐、延期后继续办理');
    const tasks=page.locator('#onboardingTable tr').filter({hasText:name});
    let finalState='joined';
    if(process.env.UI_ONBOARDING_BRANCH==='withdraw'){
      await submit(tasks.locator('form').first());
      await registration.locator('[name=action]').selectOption('withdraw');
      await registration.locator('[name=note]').fill('虚构验收：候选人明确放弃入职');
      await submit(registration); await state('onboarding_withdrawn');
      assert.equal(await tasks.locator('button[type=submit]').count(),0);
      assert.ok((await tasks.allTextContents()).join(' ').includes('已取消'));
      await shot('07-withdrawn'); steps.push('完成一项后放弃入职，未完成事项取消');
      finalState='onboarding_withdrawn';
    }else{
    while(await tasks.locator('button[type=submit]').count()) await submit(tasks.locator('form').first());
    await state('onboarding');
    await registration.locator('[name=action]').selectOption('join');
    await registration.locator('[name=actual_date]').fill(new Date().toISOString().slice(0,10));
    await registration.locator('[name=note]').fill('虚构验收：HR 已现场核实到岗');
    await shot('05-independent-arrival'); await submit(registration); await state('joined');
    await shot('06-joined'); steps.push('入职登记、四项准备完成仍未到岗、HR 独立确认到岗');
    }
    assert.deepEqual(errors,[]);
    const report={steps,errors,candidate_id:candidate.id,application_id:appId,interview_ids:rounds,offer_id:offerId,final_state:finalState,provider:'mock',external_requests:'blocked',status:'passed'};
    await fs.writeFile(path.join(output,'flow-results.json'),JSON.stringify(report,null,2)); console.log(JSON.stringify(report,null,2));
  }catch(e){await shot('failure');throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
