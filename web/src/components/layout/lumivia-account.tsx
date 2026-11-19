import { useEffect, useState } from 'react';
import { LogIn, LogOut } from 'lucide-react';
import { useConfigStore, modelOptionsFromChannels, type ModelChannel } from '@/stores/use-config-store';

type Account = { email: string; group: string; prices: Record<string, number>; models: string[] };
const channelId = 'lumivia-image';

function applyAccount(account: Account | null) {
    const config = useConfigStore.getState().config;
    const others = config.channels.filter(c => c.id !== channelId);
    const previous = config.channels.find(c => c.id === channelId);
    if (!account && !previous) return;
    const channel: ModelChannel | undefined = account ? {id: channelId, name:'乐米 · '+account.group, baseUrl: location.origin+'/lumivia', apiKey:'canvas-session', apiFormat:'openai', models:account.models.map(name=>({name,capability:'image'}))} : undefined;
    const channels = channel ? [channel, ...others] : others;
    const models = modelOptionsFromChannels(channels);
    const preferred = account?.models.includes('gpt-image-2') ? 'gpt-image-2' : account?.models[0];
    const selected = previous && models.includes(config.imageModel) && config.imageModel.startsWith(channelId+'::') ? config.imageModel : preferred ? `${channelId}::${preferred}` : others.flatMap(c=>c.models.filter(m=>m.capability==='image').map(m=>`${c.id}::${m.name}`))[0] || '';
    useConfigStore.setState({config:{...config,channels,models,channelMode:'local',imageModel:selected,model:selected,...(channel ? {proxyEnabled:false} : {})}});
}

export function LumiviaAccount(){
    const [account,setAccount]=useState<Account|null>(null);
    const [error,setError]=useState('');
    useEffect(()=>{let live=true; const sync=async()=>{try{const r=await fetch('/lumivia/session',{cache:'no-store'});if(!r.ok)throw Error();const data:Account|null=await r.json();if(live){applyAccount(data);setAccount(data);setError('')}}catch{if(live)setError('暂时无法连接主站')}};void sync();window.addEventListener('focus',sync);return()=>{live=false;window.removeEventListener('focus',sync)}},[]);
    const prices=Object.entries(account?.prices || {}).map(([k,v])=>`${k.toUpperCase()} ¥${v}`).join(' · ');
    return <div className="flex min-h-8 shrink-0 flex-wrap items-center justify-between gap-x-4 border-b border-stone-200 bg-background px-4 py-1 text-xs dark:border-stone-800" aria-label="乐米账号">
        <span className="truncate" title={account?.email}>{account ? `${account.email} · ${account.group}` : '乐米画布'} {error && <span role="status">{error}</span>}</span>
        <span className="flex items-center gap-3">{account ? <><span>{prices ? `${prices} / 张` : '按主站分组计费'}</span><button type="button" aria-label="退出画布账号" title="退出画布账号" onClick={async()=>{try{const r=await fetch('/lumivia/logout',{method:'POST'});if(!r.ok)throw Error();applyAccount(null);setAccount(null)}catch{setError('退出失败，请重试')}}}><LogOut size={15}/></button></> : <a className="inline-flex items-center gap-1 font-medium" target={window.top===window.self?'_self':'_blank'} rel="noopener" href="/lumivia/start"><LogIn size={15}/>用乐米账号登录</a>}</span>
    </div>;
}
