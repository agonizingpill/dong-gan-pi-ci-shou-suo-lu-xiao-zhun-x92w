import {store} from '../api.js';
import {badge, pageTitle, sectionTitle} from '../components/ledger.js';
import {h, number, when} from '../presentation.js';

function basisText(item) {
  return `${number(item.nearest_edge_margin, 3)} − ${number(item.loo_rmse, 3)} = ${number(item.queue_key, 3)} pp`;
}

function stationCard(station, staff) {
  const claim = station.claim;
  return `<section class="sheet station-card ${station.occupied ? 'busy' : 'idle'}">
    ${sectionTitle(station.name, station.occupied ? '占用中' : '空闲')}
    <div class="padded">
      ${claim ? `<dl class="ledger-meta">
        <div><dt>校准单</dt><dd><a class="mono" href="#release/${claim.release_id}">${h(claim.batch_code)} / #${String(claim.release_id).padStart(3, '0')}</a></dd></div>
        <div><dt>领单班次</dt><dd>${h(claim.claimed_by)}（第 ${claim.attempts} 次领取）</dd></div>
        <div><dt>领取时间</dt><dd>${when(claim.claimed_at)}</dd></div>
        <div><dt>排队依据</dt><dd class="mono">${basisText(claim)}</dd></div>
        <div><dt>状态</dt><dd>${badge(claim.state)}</dd></div></dl>
        ${claim.draft ? `<p class="callout warning">草稿试片 <b class="mono">${h(claim.draft.specimen)}</b>
          ${number(claim.draft.before_mm, 3)} → ${number(claim.draft.after_mm, 3)} mm 尚未入账，交班后自动作废。</p>` : ''}
        ${staff ? `<details class="entry-form" ${claim.draft ? 'open' : ''}><summary>登记复测读数（草稿或直接入账）</summary>
          <form data-action="retest" data-claim="${claim.id}" data-release="${claim.release_id}">
            <label>独立试片编号<input name="specimen" placeholder="TRAY-R1" value="${h(claim.draft?.specimen || '')}" required></label>
            <label>复测说明<input name="note" placeholder="交班留痕说明" value="${h(claim.draft?.note || '')}" required></label>
            <label>冻干前 mm<input name="before_mm" type="number" min="0.00001" max="200" step="0.00001" value="${claim.draft?.before_mm ?? ''}" required></label>
            <label>冻干后 mm<input name="after_mm" type="number" min="0.00001" max="200" step="0.00001" value="${claim.draft?.after_mm ?? ''}" required></label>
            <button type="button" class="text-button" data-action="draft" data-claim="${claim.id}">先存草稿</button>
            <button class="primary">读数入账并腾工位</button></form></details>
        <form data-action="returnClaim" data-claim="${claim.id}" class="return-form">
          <label class="wide">交班退回说明<input name="note" placeholder="交班退回，未测单重新排队" minlength="4" required></label>
          <button class="text-button">交班退回队列</button></form></section>` : ''}`
        : '<p class="empty">工位空闲，可从下方队列头名领单。</p>'}
    </div></section>`;
}

function queueTable(board, staff) {
  if (!board.queue.length) return '<p class="empty">当前没有等待工位的复测单。</p>';
  return `<table><thead><tr><th>排队</th><th>冻干批次 / 校准单</th><th>状态</th><th>预测 %</th>
    <th>最近窗边距离 pp</th><th>留一误差 pp</th><th>排队依据 pp</th><th></th></tr></thead><tbody>
    ${board.queue.map((item, index) => `<tr ${index === 0 ? 'class="queue-head"' : ''}>
      <th class="mono">${item.position}</th>
      <th><a class="mono" href="#release/${item.release_id}">${h(item.batch_code)} / #${String(item.release_id).padStart(3, '0')}</a></th>
      <td>${badge(item.calibration_state)}</td>
      <td class="mono">${number(item.prediction_pct)}%</td>
      <td class="mono">${number(item.nearest_edge_margin, 3)}</td>
      <td class="mono">${number(item.loo_rmse, 3)}</td>
      <td class="mono"><b>${number(item.queue_key, 3)}</b></td>
      <td>${staff ? `<button class="primary compact" data-action="claim" data-release="${item.release_id}"
        ${index === 0 ? '' : 'disabled title="仅队列头名可领，新单可按依据插队"'}>领单</button>` : ''}</td>
    </tr>`).join('')}</tbody></table>`;
}

function recentTable(board) {
  if (!board.recent_claims.length) return '<p class="empty">领单记录会在此留痕。</p>';
  return `<table><thead><tr><th>领单</th><th>工位</th><th>批次 / 校准单</th><th>班次</th><th>重试</th><th>结果</th><th>时间</th></tr></thead><tbody>
    ${board.recent_claims.map(claim => `<tr>
      <th class="mono">#${claim.id}</th><td>${h(claim.station_name)}</td>
      <td><a class="mono" href="#release/${claim.release_id}">${h(claim.batch_code)} / #${String(claim.release_id).padStart(3, '0')}</a></td>
      <td>${h(claim.claimed_by)}</td><td class="mono">${claim.attempts}</td>
      <td>${badge(claim.state)}</td><td>${when(claim.finished_at || claim.claimed_at)}</td>
    </tr>`).join('')}</tbody></table>`;
}

export function stationsPage() {
  const board = store.board;
  if (!board) return '';
  const staff = store.user.role !== 'viewer';
  return `${pageTitle('复测工位', `实验室共 ${board.station_count} 个复测工位；一单一工位，已领单完成或交班退回后才腾位。`)}
    <p class="callout">${h(board.rule)}排队依据 = 预测到最近窗边的距离 − 留一误差（LOO RMSE），数值越小越靠前。</p>
    <div class="station-grid">${board.stations.map(station => stationCard(station, staff)).join('')}</div>
    <section class="sheet queue-sheet">${sectionTitle('复测排队', `${board.queue.length} 单等待`) }
      <div class="table-scroll padded">${queueTable(board, staff)}</div></section>
    <section class="sheet queue-sheet">${sectionTitle('近期领单留痕')}
      <div class="table-scroll padded">${recentTable(board)}</div></section>`;
}
