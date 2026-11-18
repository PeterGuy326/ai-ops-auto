import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/components/PublishPage.tsx', import.meta.url), 'utf8');
const parsed = ts.createSourceFile('PublishPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

function isUseEffectCall(node) {
  return ts.isCallExpression(node)
    && ts.isIdentifier(node.expression)
    && node.expression.text === 'useEffect';
}

function isAliveAssignment(node, value) {
  return ts.isExpressionStatement(node)
    && ts.isBinaryExpression(node.expression)
    && node.expression.operatorToken.kind === ts.SyntaxKind.EqualsToken
    && node.expression.left.getText(parsed) === 'aliveRef.current'
    && node.expression.right.kind === (value ? ts.SyntaxKind.TrueKeyword : ts.SyntaxKind.FalseKeyword);
}

function statementsOfArrowBody(node) {
  return ts.isArrowFunction(node) && ts.isBlock(node.body) ? node.body.statements : [];
}

function findPublishLifecycleEffect() {
  let found;
  function visit(node) {
    if (isUseEffectCall(node)) {
      const callback = node.arguments[0];
      const statements = statementsOfArrowBody(callback);
      if (statements.some(statement => isAliveAssignment(statement, true))) found = node;
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  return found;
}

function findCallbackStatements(name) {
  let found = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === name
      && node.initializer) {
      const initializer = node.initializer;
      if (ts.isCallExpression(initializer) && initializer.expression.getText(parsed) === 'useCallback') {
        found = statementsOfArrowBody(initializer.arguments[0]);
      } else {
        found = statementsOfArrowBody(initializer);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  return found;
}

test('PublishPage only marks itself dead from the unmount cleanup', () => {
  const effect = findPublishLifecycleEffect();
  assert.ok(effect, 'missing PublishPage lifecycle effect');
  const statements = statementsOfArrowBody(effect.arguments[0]);

  assert.ok(statements.some(statement => isAliveAssignment(statement, true)),
    'mount must mark PublishPage alive before async callbacks run');
  assert.equal(statements.some(statement => isAliveAssignment(statement, false)), false,
    'mount body must not immediately mark PublishPage dead');

  const cleanup = statements.find(statement => ts.isReturnStatement(statement));
  assert.equal(cleanup?.expression?.getText(parsed), 'cleanupPublishActivity',
    'lifecycle effect must return the unmount cleanup callback');

  const cleanupStatements = findCallbackStatements('cleanupPublishActivity');
  assert.ok(cleanupStatements.some(statement => isAliveAssignment(statement, false)),
    'unmount cleanup must mark PublishPage dead');
  assert.ok(cleanupStatements.some(statement => statement.getText(parsed).includes('adaptCtl.current?.abort()')),
    'unmount cleanup must abort the active adaptation stream');
  assert.ok(cleanupStatements.some(statement => statement.getText(parsed).includes('pollTimers.current.clear()')),
    'unmount cleanup must clear publish status polling timers');
});
