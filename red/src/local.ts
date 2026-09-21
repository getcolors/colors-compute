import {chmodSync,constants,fchmodSync,fstatSync,lstatSync,mkdirSync,openSync,closeSync,fsyncSync,readSync,renameSync,rmSync,rmdirSync,writeFileSync} from 'node:fs';
import {dirname} from 'node:path';
const limit=2*1024*1024;
const absent=(error:unknown)=>(error as NodeJS.ErrnoException)?.code==='ENOENT';
export function localDirectoryValid(value:unknown):value is string {
  return typeof value==='string' && value.startsWith('/') && !/[\\\0]/.test(value) && (value==='/' || value.slice(1).split('/').every(part=>part!==''&&part!=='.'&&part!=='..'));
}
export const localPath=(directory:string,key:string)=>`${directory==='/'?'':directory}/${key}`;
export function localDirectory(opts:Record<string,unknown>):string {
  let directory:unknown;
  if(Object.hasOwn(opts,'local-state-dir')){
    directory=opts['local-state-dir'];
    if(directory==null || (typeof directory==='string' && (!directory.trim() || directory.trim().toUpperCase()==='REPLACE_ME')))
      throw new Error(':local-state-dir is required');
  }else{
    const home=process.env.HOME;
    if(!localDirectoryValid(home))throw new Error(':local-state-dir must be an absolute normalized POSIX path');
    directory=localPath(home,'.local/state/colors');
  }
  if(!localDirectoryValid(directory))throw new Error(':local-state-dir must be an absolute normalized POSIX path');
  return directory;
}

function privateDirectory(directory:string):void {
  if(directory==='/')return;
  checkParents(directory);
  try {const info=lstatSync(directory);if(!info.isDirectory()||info.isSymbolicLink())throw Error('invalid local directory');}
  catch(error){if(!absent(error))throw error;privateDirectory(dirname(directory));try{mkdirSync(directory,{mode:0o700});}catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST'||!lstatSync(directory).isDirectory()||lstatSync(directory).isSymbolicLink())throw error;}}
}
function checkParents(directory:string):void {
  if(directory==='/')return;
  checkParents(dirname(directory));
  try{const info=lstatSync(directory);if(!info.isDirectory()||info.isSymbolicLink())throw Error('invalid local directory');}catch(error){if(!absent(error))throw error;}
}
function regular(path:string):boolean {
  checkParents(dirname(path));
  try {const info=lstatSync(path);if(!info.isFile()||info.isSymbolicLink())throw Error('invalid local file');return true;}
  catch(error){if(absent(error))return false;throw error;}
}
function openRegular(path:string):number {
  checkParents(dirname(path));
  const fd=openSync(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  if(!fstatSync(fd).isFile()){closeSync(fd);throw Error('invalid local file');}
  return fd;
}
export function localPresence(path:string):{status:'present'|'absent'|'error'} {
  try {if(!regular(path))return {status:'absent'};const fd=openRegular(path);try{readSync(fd,Buffer.alloc(1),0,1,null);}finally{closeSync(fd);}return {status:'present'};}
  catch{return {status:'error'};}
}
function prepareDirectories(path:string,root?:string):void {
  privateDirectory(dirname(path));
  for(let directory=dirname(path);directory!=='/'&&directory!==root;directory=dirname(directory)){
    chmodSync(directory,0o700);
    if(root===undefined)break;
  }
}
export function prepareLocalState(path:string,root?:string):void {prepareDirectories(path,root);protectLocalState(path);}
export function protectLocalState(path:string):void {
  for(const file of [path,path+'.backup'])if(regular(file)){
    const fd=openRegular(file);try{fchmodSync(fd,0o600);}finally{closeSync(fd);}
  }
}
