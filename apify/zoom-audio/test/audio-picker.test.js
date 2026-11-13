import test from 'node:test';
import assert from 'node:assert/strict';
import {selectAudioOnly} from '../src/audio-picker.js';

function fixture({legacy=false, missingAudio=false, selected=[true,true,true]}={}) {
  let clicked=0;
  const leaf=selected.map(checked=>({checked,async isChecked(){return this.checked;},async uncheck(){this.checked=false;},async check(){this.checked=true;},async count(){return 1;}}));
  const boxes={async count(){return leaf.length;},nth(i){return leaf[i];}};
  const row={async waitFor(){if(missingAudio)throw Error('audio missing');},getByRole(){return leaf[1];}};
  const tree={filter(){return this;},first(){return this;},getByRole(role){assert.ok(['treeitem','checkbox'].includes(role));return role==='treeitem'?row:boxes;}};
  const page={locator(){return tree;},getByRole(role){return role==='heading'?{async waitFor(){if(legacy)throw Error('no picker');}}:{async count(){return 1;},async isEnabled(){return true;},async click(){assert.deepEqual(leaf.map(x=>x.checked),[false,true,false]);clicked++;}};}};
  return {page,leaf,get clicked(){return clicked;}};
}
const log={info(){}};
test('clears defaults and downloads only audio',async()=>{const f=fixture();assert.equal(await selectAudioOnly(f.page,log),true);assert.equal(f.clicked,1);});
test('selects audio when nothing is selected',async()=>{const f=fixture({selected:[false,false,false]});await selectAudioOnly(f.page,log);assert.equal(f.clicked,1);});
test('old direct-download player remains supported',async()=>{const f=fixture({legacy:true});assert.equal(await selectAudioOnly(f.page,log),false);assert.equal(f.clicked,0);});
test('never clicks Download when audio is missing',async()=>{const f=fixture({missingAudio:true});await assert.rejects(selectAudioOnly(f.page,log),/audio missing/);assert.equal(f.clicked,0);});
