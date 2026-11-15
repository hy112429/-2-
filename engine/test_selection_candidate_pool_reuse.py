# -*- coding: utf-8 -*-
"""候选池复用边界的隔离测试；不启动浏览器、不读真实账号。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class SelectionCandidatePoolReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(__file__).resolve().parents[1] / "desktop/src/renderer/src/utils/selection_candidates.ts"

    def run_node(self, expression: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "selection_candidates.cjs"
            script = f"""
const fs = require('fs');
const ts = require('D:/投广管理客户端-2/desktop/node_modules/typescript');
const source = fs.readFileSync({json.dumps(str(self.source))}, 'utf8');
const compiled = ts.transpileModule(source, {{ compilerOptions: {{ module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }}}}).outputText;
fs.writeFileSync({json.dumps(str(output))}, compiled, 'utf8');
const mod = require({json.dumps(str(output))});
{expression}
"""
            result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

    def test_same_conditions_reuse_and_condition_changes_split(self):
        expression = """
const base = {sources: {hotSpotEnabled: true, allianceRankEnabled: true, priority: ['hotSpot', 'allianceRank']}, categorySelection: {level1: ['a']}, excludedCategorySelection: {level3: ['x']}, thresholds: {commissionRate: 20, experienceScore: 4.5}, filters: {delivery: {}, product: {}, features: []}, poolPriority: ['小二严选'], bannedWords: ['屏蔽词']};
const same = {...base, thresholds: {...base.thresholds}};
const changed = {...base, thresholds: {...base.thresholds, commissionRate: 25}};
console.log(JSON.stringify({same: mod.selectionFilterFingerprint(base) === mod.selectionFilterFingerprint(same), changed: mod.selectionFilterFingerprint(base) !== mod.selectionFilterFingerprint(changed), profile: mod.selectionPoolVisibilityFingerprint('C:/Profiles/A\\\\') === mod.selectionPoolVisibilityFingerprint('c:\\\\profiles\\\\a')}));
"""
        result = self.run_node(expression)
        self.assertEqual(result, {"same": True, "changed": True, "profile": True})

    def test_account_state_is_independent_and_products_are_mutually_exclusive(self):
        expression = """
const rows = candidate => ({productId: candidate, unifiedScore: 80});
const result = mod.allocateExclusiveCandidates([
  {accountId: 1, accountName: 'A', capacity: 1, candidates: [rows('p1'), rows('p2')]},
  {accountId: 2, accountName: 'B', capacity: 1, candidates: [rows('p1'), rows('p3')]}
]);
console.log(JSON.stringify({a: result.get(1).candidates.map(x => x.productId), b: result.get(2).candidates.map(x => x.productId), skipped: result.get(2).skipped.map(x => x.productId)}));
"""
        result = self.run_node(expression)
        self.assertEqual(result["a"], ["p1"])
        self.assertEqual(result["b"], ["p3"])
        self.assertEqual(result["skipped"], ["p1"])


if __name__ == "__main__":
    unittest.main()
