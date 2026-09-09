import {readFileSync} from 'node:fs';
import recipes from '../resources/provider-recipes.json';
export function endpoint_agent(provider:string) {
 if(typeof provider!=='string'||!Object.hasOwn(recipes,provider)||!(recipes as any)[provider].application_reserved_ip)throw Error('unsupported compute endpoint capability');
 return {filename:'colors-compute-endpoint',content:readFileSync(new URL('../resources/endpoint-agent.py',import.meta.url),'utf8'),credentials:structuredClone((recipes as any)[provider].endpoint_credentials)};
}
