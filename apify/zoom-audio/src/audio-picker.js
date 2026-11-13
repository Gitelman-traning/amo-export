const pickerTitle = /^(select files to download|выберите файлы для (скачивания|загрузки))$/i;
const audioLabel = /^(audio only|только аудио|только звук)/i;

/** New Zoom opens a picker; old Zoom starts the downloads immediately. */
export async function selectAudioOnly(page, log) {
  const heading = page.getByRole('heading', { name: pickerTitle });
  try {
    await heading.waitFor({ state: 'visible', timeout: 8000 });
  } catch {
    log.info('AUDIO_PICKER: absent, keeping legacy direct download flow');
    return false;
  }

  const tree = page.locator('[role="tree"], [role="treegrid"]')
    .filter({ hasText: /audio only|только аудио|только звук/i }).first();
  const audioRow = tree.getByRole('treeitem', { name: audioLabel });
  await audioRow.waitFor({ state: 'visible', timeout: 10000 });
  const audio = audioRow.getByRole('checkbox');
  if (await audio.count() !== 1) throw new Error('AUDIO_PICKER_AUDIO_NOT_UNIQUE');

  // Do not rely on Zoom's defaults: clear video, transcript and parent selection.
  const checkboxes = tree.getByRole('checkbox');
  for (let i = 0; i < await checkboxes.count(); i++) {
    const checkbox = checkboxes.nth(i);
    if (await checkbox.isChecked()) await checkbox.uncheck({ timeout: 5000 });
  }
  await audio.check({ timeout: 5000 });
  let checked = 0;
  for (let i = 0; i < await checkboxes.count(); i++) {
    if (await checkboxes.nth(i).isChecked()) checked++;
  }
  if (!await audio.isChecked() || checked !== 1) {
    throw new Error('AUDIO_PICKER_NOT_AUDIO_ONLY');
  }
  const download = page.getByRole('button', {
    name: /^(download|скачать|загрузить)\s*(\(\s*1\s*\))?$/i,
  });
  if (await download.count() !== 1 || !await download.isEnabled()) {
    throw new Error('AUDIO_PICKER_DOWNLOAD_NOT_READY');
  }
  log.info('AUDIO_PICKER: only audio selected; clicking Download (1)');
  await download.click({ timeout: 10000 });
  return true;
}
