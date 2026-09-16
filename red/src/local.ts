import {createHash,randomUUID} from 'node:crypto';
import {chmodSync,constants,fchmodSync,fstatSync,lstatSync,mkdirSync,openSync,closeSync,fsyncSync,readSync,renameSync,rmSync,rmdirSync,writeFileSync} from 'node:fs';
import {dirname} from 'node:path';
import type {JournalGetResult,JournalPutResult} from './journal.ts';
const limit=2*1024*1024;
const absent=(error:unknown)=>(error as NodeJS.ErrnoException)?.code==='ENOENT';
export function localDirectoryValid(value:unknown):value is string {
  return typeof value==='string' && value.startsWith('/') && !/[\\\0]/.test(value) && (value==='/' || value.slice(1).split('/').every(part=>part!==''&&part!=='.'&&part!=='..'));
}
export const localPath=(directory:string,key:string)=>`${directory==='/'?'':directory}/${key}`;
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
const hash=(bytes:Uint8Array|string)=>createHash('sha256').update(bytes).digest('hex');
export function localJournalGet(path:string):JournalGetResult {
  try {
    if(!regular(path))return {status:'absent'};
    const fd=openRegular(path),buffer=Buffer.alloc(limit+1);let length=0;
    try{while(length<buffer.length){const count=readSync(fd,buffer,length,buffer.length-length,null);if(count===0)break;length+=count;}}finally{closeSync(fd);}
    if(length>limit)return {status:'error'};const bytes=buffer.subarray(0,length);
    const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes),document:unknown=JSON.parse(text);
    if(document===null||typeof document!=='object'||Array.isArray(document))return {status:'error'};
    return {status:'present',etag:hash(bytes),document};
  }catch{return {status:'error'};}
}
export function localJournalPut(path:string,condition:Record<string,any>,body:string,root?:string):JournalPutResult {
  const lock=path+'.lock',temporary=path+'.'+randomUUID()+'.tmp';let held=false;
  try {
    prepareDirectories(path,root);
    try{mkdirSync(lock,{mode:0o700});held=true;}catch(error){return {status:(error as NodeJS.ErrnoException).code==='EEXIST'?'conflict':'error'};}
    const current=localJournalGet(path);
    if(current.status==='error')return {status:'error'};
    if(condition.if_none_match==='*'?current.status!=='absent':current.status!=='present'||current.etag!==condition.if_match)return {status:'conflict'};
    const fd=openSync(temporary,'wx',0o600);
    try{writeFileSync(fd,body);fsyncSync(fd);}finally{closeSync(fd);}
    renameSync(temporary,path);
    return {status:'written',etag:hash(body)};
  }catch{return {status:'error'};}
  finally{if(held)try{rmSync(temporary,{force:true});rmdirSync(lock);}catch{return {status:'error'};}}
}
