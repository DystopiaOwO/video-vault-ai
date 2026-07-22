(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function showToast(message) {
    const toast = $('[data-toast]');
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { toast.hidden = true; }, 2200);
  }

  function setWorkspace(name) {
    $$('.workspace').forEach((panel) => {
      const active = panel.dataset.workspace === name;
      panel.hidden = !active;
      panel.classList.toggle('active', active);
    });
    $$('.nav-button').forEach((button) => button.classList.toggle('active', button.dataset.workspaceTarget === name));
    const currentStep = $(`.workflow-step[data-workspace-target="${name}"]`);
    $$('.workflow-step').forEach((step) => step.classList.remove('current'));
    if (currentStep) currentStep.classList.add('current');
    window.scrollTo?.({ top: 0, behavior: 'smooth' });
  }

  function updateIncludedTotals() {
    const included = $$('.segment-row').filter((row) => row.dataset.included === 'true').length;
    $$('[data-included-total]').forEach((node) => { node.textContent = String(included); });
    $$('[data-output-included]').forEach((node) => { node.textContent = `${included} 個片段將進入輸出`; });
  }

  function markDirty() {
    const dirty = $('[data-unsaved]');
    if (dirty) dirty.hidden = false;
  }

  function selectSegment(row) {
    if (!row) return;
    $$('.segment-row').forEach((item) => item.classList.toggle('selected', item === row));
    $('[data-inspector-title]').textContent = row.dataset.title || '';
    $('[data-inspector-source]').textContent = row.dataset.source || '';
    $('[data-inspector-score]').textContent = row.dataset.score || '0.00';
    $('[data-duration-input]').value = row.dataset.duration || '';
    $('[data-notes-input]').value = row.dataset.notes || '';
    $('[data-included-toggle]').checked = row.dataset.included === 'true';
    $('[data-locked-toggle]').checked = row.dataset.locked === 'true';
    $('[data-audio-summary]').textContent = row.dataset.audio || '使用專案設定';
    $('[data-color-summary]').textContent = row.dataset.color || '使用專案設定';

    const range = (row.dataset.range || '').split('–').map((value) => value.trim());
    $('[data-start-input]').value = range[0] || '';
    $('[data-end-input]').value = range[1] || '';

    const roleSelect = $('[data-role-select]');
    if (roleSelect) {
      const role = row.dataset.role || '';
      if (!Array.from(roleSelect.options).some((option) => option.value === role)) {
        roleSelect.add(new Option(role, role));
      }
      roleSelect.value = role;
    }

    const preview = $('[data-inspector-preview]');
    if (preview) {
      preview.className = `inspector-preview ${row.querySelector('.segment-thumb')?.classList[1] || 'shot-one'}`;
    }
  }

  function updateSelectedSegmentFromInspector() {
    const row = $('.segment-row.selected');
    if (!row) return;
    row.dataset.notes = $('[data-notes-input]').value;
    row.dataset.included = String($('[data-included-toggle]').checked);
    row.dataset.locked = String($('[data-locked-toggle]').checked);
    row.dataset.role = $('[data-role-select]').value;
    row.dataset.range = `${$('[data-start-input]').value} – ${$('[data-end-input]').value}`;
    row.classList.toggle('excluded', row.dataset.included !== 'true');

    const statusChip = row.querySelector('.state-chip');
    if (statusChip) {
      statusChip.className = `state-chip ${row.dataset.included === 'true' ? 'success' : 'muted'}`;
      statusChip.textContent = row.dataset.included === 'true' ? '已納入' : '已排除';
    }
    const timeLine = row.querySelector('.segment-main small');
    if (timeLine) timeLine.textContent = `${row.dataset.range} · 成片 ${row.dataset.duration}`;
    updateIncludedTotals();
    markDirty();
  }

  function updateApprovalAvailability() {
    const checks = $$('[data-gate-check]');
    const button = $('[data-approve-button]');
    if (button) button.disabled = !checks.every((check) => check.checked);
  }

  function approveCurrentVersion() {
    const hero = $('[data-approval-hero]');
    hero?.classList.remove('pending');
    hero?.classList.add('approved');
    const icon = hero?.querySelector(':scope > span');
    if (icon) icon.textContent = '✓';
    $('[data-approval-title]').textContent = '目前版本已核准';
    $('[data-approval-copy]').textContent = '正式輸出已開放；後續修改輸出內容時會自動失效。';
    const renderButton = $('[data-render-button]');
    if (renderButton) renderButton.disabled = false;
    $('[data-render-help]').textContent = '輸出會建立背景 Render Job，可在工作台查看進度。';
    showToast('目前版本已核准，正式輸出已開放。');
  }

  document.addEventListener('DOMContentLoaded', () => {
    $$('[data-workspace-target]').forEach((control) => {
      control.addEventListener('click', () => setWorkspace(control.dataset.workspaceTarget));
    });

    $$('.segment-row').forEach((row) => row.addEventListener('click', () => selectSegment(row)));
    selectSegment($('.segment-row.selected'));
    updateIncludedTotals();

    $$('[data-notes-input], [data-start-input], [data-end-input], [data-role-select], [data-included-toggle], [data-locked-toggle]').forEach((control) => {
      control.addEventListener('input', updateSelectedSegmentFromInspector);
      control.addEventListener('change', updateSelectedSegmentFromInspector);
    });

    $('[data-apply-segment]')?.addEventListener('click', () => {
      updateSelectedSegmentFromInspector();
      showToast('片段設定已套用到互動原型。');
    });

    $('[data-save-storyboard]')?.addEventListener('click', () => {
      const dirty = $('[data-unsaved]');
      if (dirty) dirty.hidden = true;
      showToast('分鏡已在原型中標記為儲存。');
    });

    $('[data-save-tuning]')?.addEventListener('click', () => showToast('調色與音訊設定已在原型中標記為儲存。'));

    $$('[data-preview-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        $$('[data-preview-mode]').forEach((item) => item.classList.toggle('active', item === button));
        const preview = $('[data-large-preview]');
        const mode = button.dataset.previewMode;
        preview.classList.remove('before', 'after', 'segment');
        preview.classList.add(mode);
        $('[data-preview-label]').textContent = mode === 'before' ? 'Before · 原始 D-Log 畫面' : mode === 'after' ? 'After · 套用建議值' : 'Segment Preview · 5 秒短預覽';
      });
    });

    $$('[data-settings-tab]').forEach((button) => {
      button.addEventListener('click', () => {
        $$('[data-settings-tab]').forEach((item) => item.classList.toggle('active', item === button));
        $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== button.dataset.settingsTab; });
      });
    });

    $$('[data-gate-check]').forEach((check) => check.addEventListener('change', updateApprovalAvailability));
    $('[data-approve-button]')?.addEventListener('click', approveCurrentVersion);
    $('[data-render-button]')?.addEventListener('click', () => showToast('正式輸出工作已加入佇列（原型示範）。'));
    updateApprovalAvailability();
  });
})();
