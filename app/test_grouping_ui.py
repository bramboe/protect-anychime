"""Exercise the real UI decision functions with synthetic speaker inventories."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'),'Node is needed for browser decision tests')
class GroupingUITests(unittest.TestCase):
    def run_js(self,body):
        html=(Path(__file__).parent/'index.html').read_text()
        functions='\n'.join(re.search(r'^function '+name+r'\([^\n]+',html,re.M).group(0)
            for name in ('chimeLayoutPlan','measurementsRelevant','connectionView'))
        # connectionView is multiline; include its complete function instead.
        functions=functions[:functions.index('function connectionView')]+re.search(
            r'function connectionView\(.*?\n}',html,re.S).group(0)
        result=subprocess.run(['node','-e',"const assert=require('assert/strict');\n"+functions+'\n'+body],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unused_group_separates_and_restores_without_management(self):
        self.run_js("""
const group=['media_player.a','media_player.b'];
for(const separate of [false,true]) {
 const p=chimeLayoutPlan([{ha_group_members:group,chime_separate:separate}],new Set());
 assert.equal(p.separate,!separate);assert.equal(p.preferenceOnly,true);
 assert.equal(p.needsManagement,false);assert.equal(p.preserveExisting,false);
}
""")

    def test_partial_group_plan_is_independent_of_which_member_is_selected(self):
        self.run_js("""
const group=['media_player.a','media_player.b'];
for(const selected of group){const p=chimeLayoutPlan([{ha_group_members:group,chime_separate:true}],new Set([selected]));
 assert.equal(p.preserveExisting,true);assert.equal(p.needsManagement,false);assert.equal(p.separate,false);
}
const p=chimeLayoutPlan([{ha_group_members:group,chime_separate:true}],new Set(group));
assert.equal(p.needsManagement,true);assert.equal(p.preserveExisting,false);
""")

    def test_measurements_hidden_for_single_output_but_include_native_chimes_and_groups(self):
        self.run_js("""
const single=[{entity_id:'media_player.a'}],one=[{key:'primary',active:true}];
assert.equal(measurementsRelevant(single,one,1),false);
assert.equal(measurementsRelevant(single,one,null),false);
assert.equal(measurementsRelevant(single,one,2),true);
assert.equal(measurementsRelevant([{},{}],one,1),true);
assert.equal(measurementsRelevant(single,[...one,{key:'other',active:true}],null),true);
assert.equal(measurementsRelevant(single,[...one,{key:'old',active:false}],1),false);
assert.equal(measurementsRelevant(single,[...one,...one],1),false);
""")

    def test_automatic_adoption_status_has_no_undefined_variable(self):
        self.run_js("""
const v=connectionView({chimes:[{state:'awaiting_adoption',adopted:false}],selected_entities:['media_player.a'],auto_adopt_enabled:true}, {trusted:true});
assert.equal(v.label,'Adopting in Protect');
""")
