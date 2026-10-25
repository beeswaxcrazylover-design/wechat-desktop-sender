import _support
import ast
from hashlib import sha256
import json
from pathlib import Path
import unittest


class Imports(ast.NodeTransformer):
    def visit_ImportFrom(self,node):
        node.level=0
        return node


class ExtractionIntegrityTests(unittest.TestCase):
    def test_preserved_definitions_match_audited_source_fingerprints(self):
        root=Path(__file__).resolve().parents[1]
        manifest=json.loads((root/'docs/extraction-map.json').read_text(encoding='utf-8'))
        for item in manifest['modules']:
            tree=ast.parse((root/'src/wechat_desktop_sender'/item['destination_module']).read_text(encoding='utf-8'))
            definitions={}
            def walk(node,prefix=''):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                        name=prefix+child.name
                        definitions[name]=child
                        walk(child,name+'.')
                    else:walk(child,prefix)
            walk(tree)
            for entry in item['identical_definitions']:
                with self.subTest(module=item['destination_module'],definition=entry['definition']):
                    node=Imports().visit(definitions[entry['definition']])
                    self.assertEqual(sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),entry['ast_sha256'])

