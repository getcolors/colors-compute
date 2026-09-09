import {registry} from './index.ts';
type Json = null | boolean | number | string | Json[] | {[key: string]: Json};

/** Substitute whole-value placeholders without evaluating Terraform expressions. */
export function render_template(value: Json, inputs: Record<string, Json>): Json {
  if (typeof value === 'string') {
    const match = /^\{\{([a-z_]+)\}\}$/.exec(value);
    if (match && match[0] === value) {
      const key = match[1];
      if (!Object.hasOwn(inputs, key)) throw new Error(`missing template input: ${key}`);
      return structuredClone(inputs[key]);
    }
    if (value.includes('{{') || value.includes('}}')) throw new Error('template placeholders must occupy the entire string');
    return value;
  }
  if (Array.isArray(value)) return value.map(item => render_template(item, inputs));
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, render_template(item, inputs)]));
  }
  return value;
}

/** Plan nonsecret backend configuration. Credential binding belongs to runtime. */
export function backend_plan(opts: Record<string, any>, state_key: string) {
  const backend: unknown = opts['provider-backend'];
  if (backend !== 'r2' && backend !== 's3') throw new Error(':provider-backend must be one of r2, s3');
  for (const key of [...registry.backend[backend].required].sort()) {
    const value = opts[key];
    if (value == null || (typeof value === 'string' && (!value.trim() || value.trim().toUpperCase() === 'REPLACE_ME'))) {
      throw new Error(`:${key} is required`);
    }
  }
  if (typeof state_key !== 'string' || !state_key.split('/').every(part => /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.exec(part)?.[0] === part)) {
    throw new Error('invalid state key');
  }
  const settings: Record<string, any> = {
    bucket: opts[`${backend}-bucket`], region: backend === 's3' ? opts['s3-region'] : 'auto', key: state_key, use_lockfile: true,
  };
  const credential_bindings: Record<string, string> = {};
  if (backend === 'r2') {
    Object.assign(settings, {
      endpoints: {s3: opts['r2-endpoint']}, use_path_style: false,
      skip_credentials_validation: true, skip_metadata_api_check: true,
      skip_region_validation: true, skip_requesting_account_id: true,
      skip_s3_checksum: true,
    });
    Object.assign(credential_bindings, {
      COLORS_PAR_R2_ACCESS_KEY_ID: 'access_key', COLORS_PAR_R2_SECRET_ACCESS_KEY: 'secret_key',
    });
  }
  return {config: {terraform: {backend: {s3: settings}}}, credential_bindings, environment: {}};
}
