import templateData from '../resources/templates.json';
import {render_template} from './rendering.ts';
type Json = Parameters<typeof render_template>[0];
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
}
const templates = freeze(templateData) as Record<string, Record<string, Record<string, Json>>>;

/** Render bundled provider assets; does not execute provider lifecycle. */
export function provider_plan(provider: string, stage: string, inputs: Record<string, Json>): Record<string, Json> {
  if (typeof provider !== 'string' || !Object.hasOwn(templates, provider)) throw new Error(`compute provider templates unavailable: ${typeof provider === 'string' ? provider : JSON.stringify(provider)}`);
  const stages = templates[provider];
  if (typeof stage !== 'string' || !Object.hasOwn(stages, stage)) throw new Error(`unsupported compute stage: ${typeof stage === 'string' ? stage : JSON.stringify(stage)}`);
  return Object.fromEntries(Object.entries(stages[stage]).map(([name, document]) => [name, render_template(document, inputs)]));
}
