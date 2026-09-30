import { useEffect, useMemo, useRef, useState } from 'react'
import { EvidenceFindingsPanel, SourceLimitations, sliceCodepoints } from './components/EvidenceFindingsPanel'
import type { FindingCitation, PassageResponse, QuestionDraft } from './types'
import { api } from './api'
import { QuestionConfirmationPanel } from './components/QuestionConfirmationPanel'
import { fieldsFromSpec, useQuestionFraming } from './components/useQuestionFraming'
import type { QuestionFields } from './components/useQuestionFraming'
import type { Action, Claim, Evidence, Health, Question, Run, SettlementSummary } from './types'

type View = 'create' | 'evidence' | 'simulation' | 'result'
type Agent12Demo = { case_id: string; question: QuestionDraft; allowed_answers: string[] }
type Preset = { category: string; question: string; resolve_by: string; resolution_rule: string; resolution_source: string }
const views: { id: View; number: string; label: string; sub: string }[] = [
  { id: 'create', number: '01', label: '创建预测', sub: '问题与边界' },
  { id: 'evidence', number: '02', label: '证据与模型', sub: '事实与假设' },
  { id: 'simulation', number: '03', label: '推演过程', sub: '行动与状态' },
  { id: 'result', number: '04', label: '结果与历史', sub: '结论与回放' },
]
const stages = ['question', 'evidence', 'world', 'simulation', 'review', 'forecast']
const stageNames: Record<string, string> = { queued: '等待开始', question: '定义问题', evidence: '整理证据', world: '构建世界状态', simulation: '模拟主体行动', review: '审查依据', forecast: '汇总预测', done: '已完成', failed: '运行失败', interrupted: '运行中断', partial: '部分完成' }
const statusNames: Record<string, string> = { queued: '排队中', running: '运行中', completed: '已完成', insufficient_evidence: '证据不足', scenario_only: '情景分析', partial: '部分完成', failed: '失败', interrupted: '中断' }
const statusLabel = (item: Run) => item.status === 'insufficient_evidence' && item.review?.status === 'blocked' && item.evidence.length
  ? '审查未通过' : statusNames[item.status] || item.status
const dtLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
const now = new Date()
const later = new Date(now.getTime() + 30 * 86400_000)


function Badge({ children, tone = 'soft' }: { children: React.ReactNode; tone?: string }) { return <span className={`badge badge-${tone}`}>{children}</span> }
function Empty({ title, text }: { title: string; text: string }) { return <div className="empty"><span className="empty-mark">◌</span><h3>{title}</h3><p>{text}</p></div> }
function SectionHeading({ eyebrow, title, description, right }: { eyebrow: string; title: string; description?: string; right?: React.ReactNode }) { return <div className="section-heading"><div><div className="eyebrow">{eyebrow}</div><h2>{title}</h2>{description && <p>{description}</p>}</div>{right}</div> }

function SettlementPanel({ run, summary, onSaved }: { run: Run; summary: SettlementSummary | null; onSaved: (updated: Run) => void }) {
  const [outcome, setOutcome] = useState('')
  const [sourceUrl, setSourceUrl] = useState(run.question.resolution_source?.startsWith('http') ? run.question.resolution_source : '')
  const [sourceTitle, setSourceTitle] = useState('')
  const [observedValue, setObservedValue] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [localError, setLocalError] = useState('')
  const lookback = run.evidence.some(item => item.source_type === 'exercise' && new Date(item.retrieved_at).getTime() > new Date(run.question.as_of).getTime())
  const deadlinePassed = !!run.question.resolve_by && Date.now() >= new Date(run.question.resolve_by).getTime()
  const canSettle = !run.demo && run.question.mode === 'binary' && !!run.forecast && deadlinePassed

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setLocalError('')
    setSubmitting(true)
    try {
      const updated = await api<Run>(`/runs/${run.run_id}/settlement`, { method: 'POST', body: JSON.stringify({
        outcome, source_url: sourceUrl.trim(), source_title: sourceTitle.trim(),
        observed_value: observedValue.trim(), note: note.trim(),
      }) })
      onSaved(updated)
    } catch (error) { setLocalError((error as Error).message) } finally { setSubmitting(false) }
  }

  return <section className="panel settlement-panel">
    <div className="panel-title"><h3>实际结果与评分</h3><div className="settlement-badges">{lookback && <Badge tone="amber">历史回看·非盲测</Badge>}<Badge tone={run.settlement ? 'green' : 'soft'}>{run.settlement ? '已结算' : '待结算'}</Badge></div></div>
    {run.settlement ? <div className="settlement-body">
      <div className="settlement-outcome"><span>实际结果</span><strong>{run.settlement.outcome}</strong>{run.settlement.observed_value && <span>观测值 {run.settlement.observed_value}</span>}</div>
      <p>结算来源：<a href={run.settlement.source_url} target="_blank" rel="noopener noreferrer">{run.settlement.source_title || run.settlement.source_url} ↗</a></p>
      {run.settlement.note && <p>备注：{run.settlement.note}</p>}
      {run.settlement.brier_score === null ? <p>原预测没有有效概率，因此没有 Brier 分数。</p> : <div className="settlement-score"><strong>Brier {run.settlement.brier_score.toFixed(4)}</strong><span>越低越好；50% 二元对照在本次结果上的分数为 0.2500。{run.settlement.brier_score > .25 ? '本次高于对照。' : run.settlement.brier_score < .25 ? '本次低于对照。' : '本次与对照相同。'}</span></div>}
      <small>实际结果由用户依据所填来源手动记录；原预测保持不变。单次评分不能证明概率已校准。{lookback && '该练习资料在信息截至日之后整理，存在回看偏差。'}</small>
    </div> : canSettle ? <form className="settlement-form" onSubmit={submit}>
      <p>结算时间已到。按既定规则录入实际结果和直接显示结果的来源，原预测不会改变。</p>
      <div className="field-row"><label className="field"><span>实际结果 *</span><select value={outcome} onChange={event => setOutcome(event.target.value)} required><option value="">请选择</option>{run.question.outcomes.map(value => <option value={value} key={value}>{value}</option>)}</select></label><label className="field"><span>观测值</span><input value={observedValue} onChange={event => setObservedValue(event.target.value)} placeholder="例如：1635.96 点" maxLength={200}/></label></div>
      <label className="field"><span>结算来源 URL *</span><input type="url" value={sourceUrl} onChange={event => setSourceUrl(event.target.value)} placeholder="https://..." required/></label>
      <label className="field"><span>来源标题</span><input value={sourceTitle} onChange={event => setSourceTitle(event.target.value)} placeholder="例如：上交所指数月报" maxLength={300}/></label>
      <label className="field"><span>备注</span><textarea value={note} onChange={event => setNote(event.target.value)} placeholder="可记录结算口径或数据说明" maxLength={1000} rows={2}/></label>
      {localError && <div className="alert" role="alert"><strong>结算失败</strong><span>{localError}</span></div>}
      <button className="button button-primary" disabled={submitting}>{submitting ? '保存中…' : '记录实际结果并评分'}</button>
    </form> : <p className="settlement-pending">{run.demo ? '教学虚构情境不记录官方结果。' : run.question.mode !== 'binary' ? '开放情景分析没有二元结果可结算。' : !deadlinePassed ? `结算时间为 ${new Date(run.question.resolve_by || '').toLocaleString('zh-CN')}，届时可录入实际结果。` : '报告生成后可录入实际结果。'}</p>}
    {summary && summary.scored_count > 0 && <div className="settlement-summary">已结算 {summary.settled_count} 次 · 有效评分 {summary.scored_count} 次 · 平均 Brier {summary.average_brier?.toFixed(4)} <span>仅作历史记录，不代表概率校准。</span></div>}
  </section>
}

export default function App() {
  const [view, setView] = useState<View>('create')
  const [health, setHealth] = useState<Health | null>(null)
  const [presets, setPresets] = useState<Preset[]>([])
  const [agent12Demo, setAgent12Demo] = useState<Agent12Demo | null>(null)
  const [history, setHistory] = useState<Run[]>([])
  const [settlementSummary, setSettlementSummary] = useState<SettlementSummary | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [ref, setRef] = useState<string | null>(null)
  const [citation, setCitation] = useState<FindingCitation | null>(null)
  const [passageData, setPassageData] = useState<PassageResponse | null>(null)
  const [passageError, setPassageError] = useState('')
  const drawerElement = useRef<HTMLElement>(null)
  const triggerElement = useRef<HTMLElement | null>(null)
  function openRef(id: string, exactCitation: FindingCitation | null = null) {
    triggerElement.current = document.activeElement as HTMLElement
    setCitation(exactCitation); setPassageData(null); setPassageError(''); setRef(id)
  }
  function closeRef() { setRef(null); setCitation(null) }

  const [round, setRound] = useState(1)
  const [question, setQuestion] = useState('')
  const [asOf, setAsOf] = useState(dtLocal(now))
  const [resolveBy, setResolveBy] = useState(dtLocal(later))
  const [resolutionRule, setResolutionRule] = useState('')
  const [resolutionSource, setResolutionSource] = useState('')
  const [mode, setMode] = useState<'binary' | 'scenario'>('binary')
  const [evidenceMode, setEvidenceMode] = useState<'import' | 'online' | 'reuse' | 'demo'>('import')
  const [evidence, setEvidence] = useState<Evidence[]>([])
  const [assumptions, setAssumptions] = useState('')
  const [parentRunId, setParentRunId] = useState<string | null>(null)
  function applyQuestionFields(next: QuestionFields) {
    setQuestion(next.question); setAsOf(next.asOf); setResolveBy(next.resolveBy); setResolutionRule(next.resolutionRule)
    setResolutionSource(next.resolutionSource); setMode(next.mode); setAssumptions(next.assumptions)
  }
  const framing = useQuestionFraming({ question, asOf, resolveBy, resolutionRule, resolutionSource, mode, assumptions }, applyQuestionFields, setError)


  useEffect(() => {
    Promise.all([api<Health>('/health'), api<Run[]>('/runs'), api<{ presets: Preset[]; agent12_demo?: Agent12Demo }>('/examples'), api<SettlementSummary>('/settlements/summary')]).then(([h, items, examples, summary]) => { setHealth(h); setHistory(items); setPresets(examples.presets); setAgent12Demo(examples.agent12_demo || null); setSettlementSummary(summary); if (items.length) setRun(items[0]) }).catch(e => setError(e.message))
  }, [])
  useEffect(() => {
    if (!run || !['queued', 'running'].includes(run.status)) return
    const timer = window.setInterval(() => {
      api<Run>(`/runs/${run.run_id}`).then(next => {
        setRun(next)
        if (!['queued', 'running'].includes(next.status)) api<Run[]>('/runs').then(setHistory)
      }).catch(e => setError(e.message))
    }, 2000)
    return () => window.clearInterval(timer)
  }, [run?.run_id, run?.status])

  const selected = useMemo(() => {
    if (!ref || !run) return null
    const evidence = run.evidence.find(item => item.id === ref)
    if (evidence) return { kind: '外部证据' as const, item: evidence }
    const assumption = run.world?.assumptions.find(item => item.id === ref)
    if (assumption) return { kind: '建模假设' as const, item: assumption }
    const actor = run.world?.actors.find(item => item.id === ref)
    if (actor) return { kind: '相关主体' as const, item: actor }
    const action = run.actions.find(item => item.id === ref)
    if (action) return { kind: '模拟行动' as const, item: action }
    const step = run.simulation.find(item => item.id === ref)
    if (step) return { kind: '模拟状态' as const, item: step }
    return null
  }, [ref, run])

  useEffect(() => {
    if (!ref || !run) return
    let active = true
    const evidence = run.evidence.find(e => e.id === ref)
    if (evidence?.snapshot_hash) {
      api<PassageResponse>(`/runs/${run.run_id}/evidence/${encodeURIComponent(ref)}/passages`).then(data => {
        if (!active) return
        if (citation && (data.snapshot_hash !== citation.snapshot_hash || sliceCodepoints(data.text, citation.start, citation.end) !== citation.quote)) {
          setPassageError('引文版本或位置与原文快照不匹配，未显示高亮。'); return
        }
        setPassageData(data)
      }).catch(error => { if (active) setPassageError((error as Error).message) })
    }
    return () => { active = false }
  }, [ref, run?.run_id, citation])

  useEffect(() => {
    if (!ref) return
    const drawer = drawerElement.current
    const first = drawer?.querySelector<HTMLElement>('button, a[href], select, textarea, [tabindex="0"]')
    first?.focus()
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeRef() }
      if (event.key === 'Tab' && drawer) {
        const items = Array.from(drawer.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], select, textarea, [tabindex="0"]'))
        if (items.length && event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items[items.length - 1].focus() }
        else if (items.length && !event.shiftKey && document.activeElement === items[items.length - 1]) { event.preventDefault(); items[0].focus() }
      }
    }
    document.addEventListener('keydown', handle)
    return () => { document.removeEventListener('keydown', handle); triggerElement.current?.focus() }
  }, [ref])

  function chooseRun(item: Run) { setRun(item); setView('result'); setRef(null); setRound(1) }
  function onSettlementSaved(updated: Run) {
    setRun(updated)
    setHistory(items => items.map(item => item.run_id === updated.run_id ? updated : item))
    api<SettlementSummary>('/settlements/summary').then(setSettlementSummary).catch(error => setError(error.message))
  }
  function showReview() {
    setView('simulation')
    window.requestAnimationFrame(() => document.getElementById('review-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
  function loadAgent12Demo() {
    if (!agent12Demo) return
    framing.newDraft(); applyQuestionFields(fieldsFromSpec(agent12Demo.question)); framing.setDemoCaseId(agent12Demo.case_id)
    setEvidenceMode('demo'); setEvidence([]); setParentRunId(null); setView('create'); setError('')
    // Loading fills a form below the fold: make the next action visible and focused.
    window.requestAnimationFrame(() => {
      const nextAction = document.getElementById('analyze-question')
      nextAction?.focus({ preventScroll: true })
      nextAction?.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
        block: 'center',
      })
    })
  }
  function chooseEvidenceMode(next: 'import' | 'online' | 'reuse') {
    if (framing.demoCaseId) framing.newDraft()
    setEvidenceMode(next)
  }
  function applyPreset(item: Preset) {
    framing.newDraft()
    setQuestion(item.question); setResolveBy(dtLocal(new Date(item.resolve_by)))
    setResolutionRule(item.resolution_rule); setResolutionSource(item.resolution_source)
    setMode('binary'); setParentRunId(null); setEvidence([])
    setEvidenceMode(health?.search_configured ? 'online' : 'import'); setError('')
  }
  function useRunForRevision() {
    if (!run) return
    framing.newDraft()
    const q = run.question
    setQuestion(q.question); setAsOf(dtLocal(new Date(q.as_of)))
    setResolveBy(q.resolve_by ? dtLocal(new Date(q.resolve_by)) : dtLocal(later))
    setResolutionRule(q.resolution_rule); setResolutionSource(q.resolution_source || '')
    setMode(q.mode); setEvidenceMode('reuse'); setEvidence([])
    setAssumptions(q.user_assumptions.join('\n')); setParentRunId(run.run_id)
    if (run.demo && run.question_framing) {
      framing.install(run.question_framing); framing.editDecisions(); setEvidenceMode('demo')
    }
    setView('create'); setError('')
  }
  async function startDemo() {
    setError(''); setBusy(true)
    try {
      const examples = await api<{ demo: { question: Question; evidence: Evidence[] } }>('/examples')
      const created = await api<{ run_id: string }>('/runs', { method: 'POST', body: JSON.stringify({ question: examples.demo.question, evidence_mode: 'demo' }) })
      setRun(await api<Run>(`/runs/${created.run_id}`)); setView('simulation')
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function startRun() {
    setError(''); setBusy(true)
    try {
      if (!framing.confirmationId) throw new Error('请先分析问题并确认当前版本。')
      if (evidenceMode === 'import' && evidence.length === 0) throw new Error('请先导入证据包；也可以选择在线检索或教学演示。')
      const created = await api<{ run_id: string }>('/runs', { method: 'POST', body: JSON.stringify({ confirmation_id: framing.confirmationId, evidence_mode: evidenceMode, evidence, parent_run_id: parentRunId }) })
      setRun(await api<Run>(`/runs/${created.run_id}`)); setView('simulation'); setParentRunId(null)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function resumeRun() {
    if (!run) return
    setError(''); setBusy(true)
    try {
      await api<{ run_id: string }>(`/runs/${run.run_id}/resume`, { method: 'POST' })
      setRun(await api<Run>(`/runs/${run.run_id}`)); setView('simulation')
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  async function uploadEvidence(file?: File) {
    if (!file) return
    try {
      const parsed = JSON.parse(await file.text())
      const items = Array.isArray(parsed) ? parsed : parsed.evidence
      if (!Array.isArray(items)) throw new Error('JSON 需要是证据数组或含 evidence 数组的对象')
      setEvidence(items); setError('')
    } catch (e) { setError(`证据包无效：${(e as Error).message}`) }
  }
  function refs(ids: string[]) { return ids.map(id => <button className="ref" key={id} onClick={() => openRef(id)}>{id}</button>) }
  function claimList(claims: Claim[], empty: string) { return claims.length ? claims.map((c, i) => <div className="claim" key={i}><div className="claim-dot"/><div><p>{c.text}</p><div className="ref-row">{refs([...c.evidence_ids, ...c.assumption_ids, ...c.simulation_ids])}</div></div></div>) : <p className="muted">{empty}</p> }
  const activeStep = run?.simulation.find(s => s.round === round)
  const activeActions: Action[] = run?.actions.filter(a => a.round === round) || []
  const runStopped = !!run && ['failed', 'partial', 'interrupted'].includes(run.status)
  const failedStage = run?.failed_stage || (runStopped ? stages.find(stage => !run?.stage_outputs[stage]) : null)
  const runError = `${failedStage ? `${stageNames[failedStage] || failedStage}阶段：` : ''}${run?.errors.join('；') || '运行在此阶段中断，可从失败阶段继续。'}`
  const reviewBlocked = run?.review?.status === 'blocked'
  const reviewReason = run?.review?.issues.find(issue => issue.severity === 'high') || run?.review?.issues[0]
  const probabilityBasis = run?.forecast?.probability_basis || run?.review?.probability_basis
  const nearEven = !!run?.forecast?.probabilities && run.question.mode === 'binary' && Math.max(...Object.values(run.forecast.probabilities)) <= .55 + 1e-9
  const lookback = !!run && run.evidence.some(item => item.source_type === 'exercise' && new Date(item.retrieved_at).getTime() > new Date(run.question.as_of).getTime())
  const stageDurations = stages.flatMap(stage => {
    const seconds = run?.stage_durations?.[stage]
    return seconds === undefined ? [] : [{ stage, seconds }]
  })

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-symbol">F<span>·</span></div><div><strong>ForecastLab</strong><small>AGENTIC FORECASTING</small></div></div>
      <div className="nav-label">WORKSPACE / 工作台</div>
      <nav className="nav-list">{views.map(v => <button key={v.id} className={view === v.id ? 'nav-item active' : 'nav-item'} onClick={() => setView(v.id)}><span>{v.number}</span><div><strong>{v.label}</strong><small>{v.sub}</small></div><b>↗</b></button>)}</nav>
      <div className="sidebar-spacer"/>
      <div className="system-card"><span className="live-dot"/><div><strong>本地工作环境</strong><small>{health?.model_configured ? `模型 ${health.model} 已配置` : '模型密钥尚未配置'}</small><small>{health?.search_configured ? '在线检索已配置' : '可使用导入证据包'}</small></div></div>
      <div className="sidebar-foot">DASC7606C · TRACK 02<br/>DECITRON-INSPIRED / V0.1</div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="breadcrumb">FORECASTLAB <span>/</span> {views.find(v => v.id === view)?.label}</div><div className="top-actions"><Badge tone={run?.demo ? 'amber' : 'soft'}>{run?.demo ? '教学虚构情境' : '课程项目'}</Badge><span className="top-time">{new Date().toLocaleDateString('zh-CN')}</span></div></header>
      <div className="content">
        {error && <div className="alert" role="alert"><strong>需要处理</strong><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
        {view === 'create' && <>
          <div className="hero"><div className="hero-copy"><div className="eyebrow light">QUESTION → EVIDENCE → SIMULATION → FORECAST</div><h1>让判断有依据，<br/><em>让推演可追溯。</em></h1><p>从一个可结算的问题开始。区分外部证据、建模假设与模拟行动，再形成带引用的主观预测。</p><div className="hero-actions"><button className="button button-light" onClick={startDemo} disabled={busy}>运行教学演示 <span>↗</span></button>{agent12Demo && <button className="button button-light" onClick={loadAgent12Demo} disabled={busy || framing.busy}>体验问题与证据新流程</button>}<span>无需 API Key · 虚构材料 · 完整流程</span></div></div><div className="hero-graphic" aria-hidden="true"><div className="orbit orbit-1"/><div className="orbit orbit-2"/><div className="orb orb-a">E</div><div className="orb orb-b">A</div><div className="orb orb-c">O</div><div className="graphic-center">S <span>→</span> A <span>→</span> O</div><div className="graphic-label">STATE · ACTION · OUTCOME</div></div></div>
          <div className="metrics"><div><strong>03</strong><span>默认模拟主体</span></div><div><strong>02</strong><span>行动与状态轮次</span></div><div><strong>01</strong><span>统一证据链</span></div><div><strong>∞</strong><span>可回放的版本记录</span></div></div>
          <div className="preset-strip"><span>跨品类示例 / QUICK START</span>{presets.map(item => <button key={item.category} onClick={() => applyPreset(item)}><strong>{item.category}</strong><small>{item.question}</small><b>↗</b></button>)}</div>
          <SectionHeading eyebrow="01 / DEFINE THE QUESTION" title="创建一次预测" description="先定义目标和结算方式。开放问题可切换为情景分析。" right={<Badge>新运行</Badge>}/>
          <div className="form-grid"><section className="panel form-panel"><label className="field"><span>预测问题 <b>*</b></span><textarea value={question} onChange={e => setQuestion(e.target.value)} placeholder="例如：某产品能否在 12 月 20 日前发布正式版？" rows={3}/><small>用可以被真实结果核对的句子提问。</small></label><div className="field-row"><label className="field"><span>信息截至时间</span><input type="datetime-local" value={asOf} onChange={e => setAsOf(e.target.value)}/></label><label className="field"><span>结算时间</span><input type="datetime-local" value={resolveBy} onChange={e => setResolveBy(e.target.value)} disabled={mode === 'scenario'}/></label></div><label className="field"><span>结算规则 <b>{mode === 'binary' ? '*' : ''}</b></span><input value={resolutionRule} onChange={e => setResolutionRule(e.target.value)} placeholder="什么情况算“是”？由哪个记录核对？"/></label><label className="field"><span>结算来源</span><input value={resolutionSource} onChange={e => setResolutionSource(e.target.value)} placeholder="官方网站、公告或赛事记录"/></label><label className="field"><span>用户指定的情景条件（每行一条，不代表事实）</span><textarea value={assumptions} onChange={e => setAssumptions(e.target.value)} placeholder="可选：用于修改条件并重跑" rows={2}/></label></section>
          <section className="panel options-panel"><div className="panel-head"><span className="panel-index">A</span><div><h3>分析模式</h3><p>根据问题形态选择输出。</p></div></div><div className="option-stack"><button className={mode === 'binary' ? 'option selected' : 'option'} onClick={() => setMode('binary')}><span className="radio"/><div><strong>二元事件预测</strong><small>输出“是 / 否”主观概率，需要截止时间与结算规则。</small></div></button><button className={mode === 'scenario' ? 'option selected' : 'option'} onClick={() => setMode('scenario')}><span className="radio"/><div><strong>开放情景分析</strong><small>描述可能路径，不强行给出可评分概率。</small></div></button></div><div className="divider"/><div className="panel-head"><span className="panel-index">B</span><div><h3>证据入口</h3><p>资料由后端记录，模型只能引用编号。</p></div></div><div className="segmented"><button className={evidenceMode === 'import' ? 'on' : ''} onClick={() => chooseEvidenceMode('import')}>导入证据包</button><button className={evidenceMode === 'online' ? 'on' : ''} onClick={() => chooseEvidenceMode('online')}>在线检索</button>{parentRunId && <button className={evidenceMode === 'reuse' ? 'on' : ''} onClick={() => chooseEvidenceMode('reuse')}>沿用证据</button>}</div>{evidenceMode === 'demo' ? <p className="framing-warning">固定教学证据 · 不联网，不产生付费调用。仅接受样例问题；澄清回答请填写“可下载的正式版”。切换证据入口会退出教学模式。</p> : evidenceMode === 'import' ? <label className="upload"><span>↑</span><strong>选择 JSON 证据包</strong><small>{evidence.length ? `已载入 ${evidence.length} 条证据` : '使用 Evidence[] 或 { evidence: [...] } 格式'}</small><input type="file" accept=".json,application/json" onChange={e => uploadEvidence(e.target.files?.[0])}/></label> : evidenceMode === 'reuse' ? <p className="hint">沿用父运行保存的证据快照，方便只修改假设或结算条件后比较结果。</p> : <p className="hint">在线模式使用 Tavily API。若未配置检索密钥，先选择导入证据包。</p>}{/(指数|股价|股票|股指|A股|科创50)/.test(question) && <p className="hint">市场方向预测可补充截至日已发布的同期限历史涨跌、波动与回撤、估值等资料；仅有一两个价格点很难说明一个月的基准概率。</p>}<button className="button button-primary full" onClick={startRun} disabled={busy || framing.busy || !framing.confirmationId}>{busy ? '正在提交…' : parentRunId ? '创建新版本并运行 →' : '开始预测 →'}</button><p className="form-note">真实运行需要模型 API Key。模型输出与来源将在完成后单独展示。</p></section></div>
          <QuestionConfirmationPanel demoQuestion={framing.demoCaseId ? question : undefined} framing={framing.framing} confirmationId={framing.confirmationId} busy={busy || framing.busy} dirty={framing.dirty}
            onAnalyze={() => framing.analyze()} onAnswer={framing.analyze} onConfirm={framing.confirm} onEdit={framing.editDecisions} onReload={framing.reload}/>
        </>}

        {view === 'evidence' && <><SectionHeading eyebrow="02 / SOURCE & MODEL" title="证据与世界状态" description="每项事实、假设和模拟都保留不同身份；点击编号查看原文。" right={run && <Badge tone={run.demo ? 'amber' : 'soft'}>{run.demo ? '虚构样例' : `${run.evidence.length} 条证据`}</Badge>}/>{run && <EvidenceFindingsPanel key={run.run_id} framing={run.question_framing} assessment={run.evidence_assessment} evidence={run.evidence} onInspectCitation={c => openRef(c.evidence_id, c)}/>}{!run ? <Empty title="还没有运行记录" text="从创建预测开始，或运行教学演示查看完整结构。"/> : <div className="columns"><div className="wide-stack"><section className="panel"><div className="panel-title"><h3>外部证据 <span>E / {run.evidence.length}</span></h3><small>来源、原文与时间</small></div>{run.evidence.length ? run.evidence.map(e => <button className="evidence-row" key={e.id} onClick={() => openRef(e.id)}><span className="id-chip">{e.id}</span><div><strong>{e.title}</strong><p>{e.excerpt}</p><small>{e.publisher || '未知发布方'} · {run.demo ? '教学虚构' : e.source_type === 'exercise' ? '历史练习' : e.source_type} · {new Date(e.retrieved_at).toLocaleDateString('zh-CN')}</small></div><span className="arrow">↗</span></button>) : <Empty title="暂无证据" text="证据阶段完成后会显示来源；无有效证据不会输出概率。"/>}</section><section className="panel"><div className="panel-title"><h3>世界状态 <span>S₀</span></h3><small>{run.world?.summary || '等待构建'}</small></div>{run.world && <div className="variable-grid">{Object.entries(run.world.variables).map(([k, v]) => <div className="variable" key={k}><small>{k.replaceAll('_', ' ')}</small><strong>{v}</strong></div>)}</div>}</section></div><div className="narrow-stack"><section className="panel"><div className="panel-title"><h3>建模假设 <span>H</span></h3><small>可修订条件</small></div>{run.world?.assumptions.length ? run.world.assumptions.map(a => <button className="assumption" key={a.id} onClick={() => openRef(a.id)}><span className="id-chip amber">{a.id}</span><div><strong>{a.content}</strong><small>{a.created_by === 'model' ? '模型提出' : '用户提出'} · 依据 {a.parent_ids.join('、') || '未记录'}</small></div></button>) : <p className="muted padded">尚无假设记录。</p>}<button className="text-button" onClick={useRunForRevision}>修改条件并创建新运行 ↗</button></section><section className="panel"><div className="panel-title"><h3>相关主体 <span>A / {run.world?.actors.length || 0}</span></h3><small>目标、约束与可见证据</small></div>{run.world?.actors.map(a => <div className="actor-mini" key={a.id}><span className="actor-avatar">{a.name.slice(0, 1)}</span><div><strong>{a.name}</strong><small>目标：{a.goal}</small><div className="ref-row">{refs(a.visible_evidence_ids)}</div></div></div>)}{run.world && !run.world.actors.length && <p className="muted padded">{run.world.simulation_branch_reason || '该问题未识别出适合行动推演的主体。'}</p>}</section>{run.evidence_assessment && <section className="panel"><div className="panel-title"><h3>证据评估</h3><small>冲突与缺口</small></div><div className="assessment"><p>{run.evidence_assessment.summary}</p><strong>冲突</strong><ul>{run.evidence_assessment.conflicts.map((x,i) => <li key={i}>{x}</li>)}</ul><strong>缺口</strong><ul>{run.evidence_assessment.gaps.map((x,i) => <li key={i}>{x}</li>)}</ul></div></section>}</div></div>}</>}

        {view === 'simulation' && <><SectionHeading eyebrow="03 / SIMULATION TRACE" title="推演过程" description="同一轮主体读取相同状态，统一由环境推进器产生下一状态。" right={run && <Badge tone={run.status === 'running' ? 'green' : 'soft'}>{statusLabel(run)}</Badge>}/>{!run ? <Empty title="等待第一条轨迹" text="运行预测后，这里会展示真实阶段、主体行动与审查结果。"/> : <><div className="run-banner"><div><span className="live-dot"/><strong>{stageNames[run.stage] || run.stage}</strong><small>{run.demo ? '教学演示 · 虚构材料' : `${run.model} · ${run.usage.calls} 次模型调用`}</small></div><span>{run.run_id}</span></div><div className="timeline">{stages.map((s, i) => <div className={run.stage_outputs[s] ? 'timeline-step complete' : runStopped && failedStage === s ? 'timeline-step failed' : run.stage === s ? 'timeline-step current' : 'timeline-step'} key={s}><span>{String(i + 1).padStart(2, '0')}</span><strong>{stageNames[s]}</strong><small>{run.stage_outputs[s] ? '已保存' : runStopped && failedStage === s ? '已中断' : run.stage === s ? '进行中' : '等待'}</small></div>)}</div>{run.errors.length > 0 && <div className="alert"><strong>运行说明</strong><span>{run.errors.join('；')}</span></div>}<div className="two-col"><section className="panel"><div className="panel-title"><h3>主体行动</h3><div className="round-tabs"><button className={round === 1 ? 'on' : ''} onClick={() => setRound(1)}>第 1 轮</button><button className={round === 2 ? 'on' : ''} onClick={() => setRound(2)}>第 2 轮</button></div></div>{activeActions.length ? activeActions.map(a => <div className="action-card" key={a.id}><div className="action-head"><span className="actor-avatar">{run.world?.actors.find(x => x.id === a.actor_id)?.name.slice(0, 1) || 'A'}</span><div><strong>{run.world?.actors.find(x => x.id === a.actor_id)?.name || a.actor_id}</strong><small>读取状态 S{a.parent_state} · 模拟行动</small></div><span className="id-chip simulation">{a.id}</span></div><h4>{a.action}</h4><p>{a.rationale_summary}</p><small>预期影响：{a.expected_impact}</small><div className="ref-row">{refs([...a.evidence_ids, ...a.assumption_ids])}</div></div>) : <Empty title="行动尚未生成" text="完成世界建模后，主体行动会在这里逐轮出现。"/>}</section><section className="panel"><div className="panel-title"><h3>环境推进 <span>S{round - 1} → S{round}</span></h3><small>统一处理相互影响</small></div>{activeStep ? <div className="step-content"><div className="step-icon">S{round}</div><h4>{activeStep.summary}</h4><div className="step-block"><small>状态变化</small>{Object.entries(activeStep.state_changes).map(([k, v]) => <p key={k}>{k.replaceAll('_', ' ')} <span>→</span> {v}</p>)}</div><div className="step-block"><small>保留的冲突</small>{activeStep.conflicts.map((c, i) => <p key={i}>{c}</p>)}</div><div className="step-block"><small>未解决问题</small>{activeStep.unresolved.map((u, i) => <p key={i}>{u}</p>)}</div><div className="ref-row">{refs([...activeStep.evidence_ids, ...activeStep.assumption_ids])}</div></div> : <Empty title="等待环境推进" text="环境会在收齐本轮所有行动后更新模拟状态。"/>}</section></div><section id="review-panel" className="panel review-panel"><div className="panel-title"><h3>审查意见</h3><Badge tone={run.review?.status === 'blocked' || (runStopped && !run.review) ? 'red' : 'soft'}>{run.review?.status || (runStopped ? '未生成' : '等待审查')}</Badge></div>{run.review ? <div className="issues">{run.review.issues.length ? run.review.issues.map((issue, i) => <div className="issue" key={i}><Badge tone={issue.severity === 'high' ? 'red' : 'amber'}>{issue.severity}</Badge><div><strong>{issue.claim}</strong><p>{issue.explanation}</p><div className="ref-row">{refs(issue.affected_ids)}</div></div></div>) : <p className="muted">没有记录到审查问题。</p>}</div> : runStopped ? <div className="review-stopped"><div className="alert" role="alert"><strong>审查意见尚未生成（{statusLabel(run)}）</strong><span>{runError}</span></div><button className="text-button" disabled={busy} onClick={resumeRun}>从失败阶段继续 ↗</button></div> : <p className="muted">尚未进入审查阶段。</p>}</section></>}</>}

        {view === 'result' && <><SectionHeading eyebrow="04 / FORECAST & HISTORY" title="结果与历史" description="主观概率、正反依据和每次运行均可回看。" right={run?.forecast && <div className="export-actions"><a className="button button-outline" href={`/api/runs/${run.run_id}/export?format=html`}>导出报告 ↗</a><a className="button button-outline" href={`/api/runs/${run.run_id}/export?format=json`}>导出 JSON ↓</a></div>}/>{!run ? <Empty title="暂无预测报告" text="先运行教学演示，或提交自己的问题与证据。"/> : <div className="result-grid"><div className="wide-stack"><section className="result-hero"><div className="eyebrow light">FORECAST / {run.demo ? 'CLASSROOM EXERCISE' : run.run_id}</div><h2>{run.question.question}</h2><p>{run.forecast?.conclusion || (runStopped ? '本次运行尚未生成预测报告，请查看下方失败原因。' : '预测正在生成，完成后将在这里展示。')}</p><div className="result-meta"><span>信息截至 {new Date(run.question.as_of).toLocaleString('zh-CN')}</span><span>{statusLabel(run)}</span><span>{run.demo ? '虚构材料' : '真实运行'}</span>{lookback && <span>历史回看·非盲测</span>}</div></section>{runStopped && <div className="alert result-error" role="alert"><strong>运行{statusLabel(run)}</strong><span>{runError}</span></div>}<section className="panel probability-panel"><div className="panel-title"><h3>结果概率</h3><Badge>主观判断 · 未校准</Badge></div>{run.forecast?.probabilities ? <><div className="probabilities">{Object.entries(run.forecast.probabilities).map(([name, p]) => <div className="probability" key={name}><div className="prob-head"><span>{name}</span><strong>{Math.round(p * 100)}%</strong></div><div className="bar"><div style={{ width: `${p * 100}%` }}/></div></div>)}</div>{nearEven && <div className="probability-note near-even" role="note"><strong>接近五五开 · 无明显方向优势</strong><p>最大结果概率不超过 55%，仍有很大的不确定性。</p></div>}{probabilityBasis === 'evidence_only' && <div className="probability-note" role="note"><strong>仅依据截至日证据</strong><p>仅使用截至日已有的外部证据，未采用建模推演。该概率是未经校准的主观估计。</p><button className="text-button" onClick={showReview}>查看审查意见 ↗</button></div>}</> : <div className="no-prob"><strong>{reviewBlocked ? '审查未通过，未输出概率' : '未输出有效概率'}</strong>{reviewBlocked ? <><p>{reviewReason ? `${reviewReason.claim}：${reviewReason.explanation}` : run.review?.unsupported_claims[0] || '审查认为现有依据不足以支持概率判断。'}</p><button className="text-button" onClick={showReview}>查看审查意见 ↗</button></> : <p>{run.forecast?.status === 'scenario_only' ? '开放问题仅提供情景分析。' : runStopped ? '运行已停止。查看上方错误原因，可从失败阶段继续。' : !run.forecast ? '预测正在生成。' : run.evidence.length === 0 ? '没有截至信息日的可核查证据。' : '现有资料未能支持概率判断，请查看报告局限。'}</p>}</div>}</section><SettlementPanel key={run.run_id} run={run} summary={settlementSummary} onSaved={onSettlementSaved}/><div className="two-col"><section className="panel"><div className="panel-title"><h3>支持依据</h3><small>点击编号展开来源</small></div>{claimList(run.forecast?.supporting || [], '暂无可展示的支持依据。')}</section><section className="panel"><div className="panel-title"><h3>反对依据</h3><small>保留不利资料与分歧</small></div>{claimList(run.forecast?.opposing || [], '暂无可展示的反对依据。')}</section></div><section className="panel"><div className="panel-title"><h3>其他情景与局限</h3><small>哪些信息可能改变结论</small></div><div className="detail-grid"><div><h4>可能情景</h4><ul>{run.forecast?.scenarios.map((x, i) => <li key={i}>{x}</li>)}</ul></div><div><h4>需要关注</h4><ul>{run.forecast?.new_information.map((x, i) => <li key={i}>{x}</li>)}</ul></div><div><h4>局限</h4><ul>{run.forecast?.limitations.map((x, i) => <li key={i}>{x}</li>)}</ul></div></div></section></div><div className="narrow-stack"><section className="panel"><div className="panel-title"><h3>当前运行</h3><small>可追溯元数据</small></div><div className="metadata"><div><span>运行编号</span><strong>{run.run_id}</strong></div><div><span>模型</span><strong>{run.model}</strong></div><div><span>调用次数</span><strong>{run.usage.calls}</strong></div><div><span>输入 / 输出 tokens</span><strong>{run.usage.prompt_tokens} / {run.usage.completion_tokens}</strong></div><div><span>状态</span><strong>{statusLabel(run)}</strong></div>{stageDurations.map(({ stage, seconds }) => <div key={stage}><span>{stageNames[stage]}耗时</span><strong>{seconds.toFixed(1)} 秒</strong></div>)}</div>{["failed", "partial", "interrupted"].includes(run.status) && <button className="text-button" disabled={busy} onClick={resumeRun}>从失败阶段继续 ↗</button>}<button className="text-button" onClick={useRunForRevision}>修改假设并重跑 ↗</button></section><section className="panel"><div className="panel-title"><h3>历史运行</h3><small>点击切换回放</small></div><div className="history-list">{history.map(item => <button className={item.run_id === run.run_id ? 'history-item selected' : 'history-item'} key={item.run_id} onClick={() => chooseRun(item)}><span className="history-dot"/><div><strong>{item.question.question}</strong><small>{new Date(item.started_at).toLocaleString('zh-CN')} · {statusLabel(item)}</small></div></button>)}</div></section></div></div>}</>}
      </div>
      <footer className="main-footer"><span>FORECASTLAB / EVIDENCE FIRST, SIMULATION SECOND.</span><span>课程实验工具 · 预测有不确定性</span></footer>
    </main>
    {ref && <div className="drawer-backdrop" onClick={closeRef}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="来源与引用详情" ref={drawerElement} onClick={e => e.stopPropagation()}>
        <div className="drawer-top"><span>REFERENCE / {ref}</span><button onClick={closeRef} aria-label="关闭详情">×</button></div>
        {selected ? <>
          <Badge tone={selected.kind === '建模假设' ? 'amber' : selected.kind === '模拟状态' || selected.kind === '模拟行动' ? 'green' : 'soft'}>{selected.kind}</Badge>
          <h2>{selected.kind === '外部证据' ? selected.item.title : selected.kind === '建模假设' ? selected.item.content : selected.kind === '相关主体' ? selected.item.name : selected.kind === '模拟行动' ? selected.item.action : selected.item.summary}</h2>
          {selected.kind === '外部证据' && <>
            <div className="drawer-meta"><div>发布方 <strong>{selected.item.publisher || '未知'}</strong></div><div>资料类型 <strong>{selected.item.source_type}</strong></div><div>抓取/导入时间 <strong>{new Date(selected.item.retrieved_at).toLocaleString('zh-CN')}</strong></div><div>内容哈希 <strong>{selected.item.content_hash.slice(0, 16)}…</strong></div></div>
            <SourceLimitations evidence={selected.item}/>
            {selected.item.source_group_basis && <><h3>来源分组依据（不等于独立性已验证）</h3><p>{selected.item.source_group_basis}</p></>}
            {selected.item.source_kind_basis && <p>{selected.item.source_kind_basis}</p>}
            <div className="drawer-meta"><div>来源发布日期<strong>{selected.item.published_at ? new Date(selected.item.published_at).toLocaleString('zh-CN') : '未知'}</strong></div><div>更新时间<strong>{selected.item.updated_at ? new Date(selected.item.updated_at).toLocaleString('zh-CN') : '未知'}</strong></div>
              {Object.entries(selected.item.date_basis || {}).map(([key,value]) => <div key={key}>日期依据 · {key}<strong>{value}</strong></div>)}
              {selected.item.snapshot_hash && <div>正文快照哈希<strong>{selected.item.snapshot_hash}</strong></div>}</div>
            {passageError && <p role="alert" className="framing-warning">{passageError}</p>}
            <h3>{citation ? '引用所在的原文上下文' : '保存的原文片段'}</h3>
            {citation && passageData ? <blockquote className="source-exact-text">
              {sliceCodepoints(passageData.text, Math.max(0, citation.start - 150), citation.start)}<mark>{sliceCodepoints(passageData.text, citation.start, citation.end)}</mark>{sliceCodepoints(passageData.text, citation.end, citation.end + 150)}
            </blockquote> : <blockquote>{selected.item.excerpt}</blockquote>}
            {citation && !passageData && !passageError && <p role="status">正在核对原文快照…</p>}
            {passageData && <details><summary>展开本次保存的来源全文{passageData.content_truncated ? '（已截断）' : ''}</summary><pre className="source-full-text">{passageData.text}</pre></details>}
            {!!selected.item.aliases?.length && <details><summary>重复网址与别名（{selected.item.aliases.length}）</summary>{selected.item.aliases.map((a,i) => <p key={i}>{a.source_url}<small> · {a.published_at || '发布日期未知'}</small></p>)}</details>}
            {!!selected.item.possible_same_source?.length && <p className="framing-warning">疑似同源，尚未合并：{selected.item.possible_same_source.join('；')}</p>}
            {selected.item.source_url && <a href={selected.item.source_url} target="_blank" rel="noopener noreferrer" className="button button-primary">查看原始来源 ↗</a>}
          </>}
          {selected.kind === '建模假设' && <><p>{selected.item.rationale}</p><div className="drawer-meta"><div>提出者 <strong>{selected.item.created_by === 'user' ? '用户' : '模型'}</strong></div></div>{selected.item.parent_ids.length > 0 && <><h3>依赖</h3><div className="ref-row">{refs(selected.item.parent_ids)}</div></>}</>}
          {selected.kind === '相关主体' && <><p>目标：{selected.item.goal}</p><div className="drawer-meta"><div>可用资源 <strong>{selected.item.resources.join('；') || '未记录'}</strong></div><div>约束 <strong>{selected.item.constraints.join('；') || '未记录'}</strong></div></div>{selected.item.visible_evidence_ids.length > 0 && <><h3>可见证据</h3><div className="ref-row">{refs(selected.item.visible_evidence_ids)}</div></>}</>}
          {selected.kind === '模拟行动' && <><p>{selected.item.rationale_summary}</p><div className="drawer-meta"><div>行动主体 <strong>{selected.item.actor_id}</strong></div><div>模拟轮次 <strong>第 {selected.item.round} 轮</strong></div><div>预期影响 <strong>{selected.item.expected_impact}</strong></div></div><h3>引用</h3><div className="ref-row">{refs([...selected.item.evidence_ids, ...selected.item.assumption_ids])}</div></>}
          {selected.kind === '模拟状态' && <><div className="drawer-meta">{Object.entries(selected.item.state_changes).map(([key, value]) => <div key={key}>{key} <strong>{value}</strong></div>)}</div>{selected.item.conflicts.length > 0 && <><h3>保留的冲突</h3><p>{selected.item.conflicts.join('；')}</p></>}{selected.item.unresolved.length > 0 && <><h3>未解决问题</h3><p>{selected.item.unresolved.join('；')}</p></>}<h3>引用</h3><div className="ref-row">{refs([...selected.item.evidence_ids, ...selected.item.assumption_ids])}</div></>}
        </> : <Empty title="未找到引用" text="该编号未出现在当前运行中。"/>}
      </aside>
    </div>}
  </div>
}
