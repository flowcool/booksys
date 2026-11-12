"""Concrete source-record mapping for the offline PostgreSQL importer."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import yaml

from library.codec import decode, encode
from library.primitives import LibraryError, digest
from library.stage_model import Task


def load(path: Path) -> dict[str,Any]:
    value=yaml.load(path.read_bytes(),Loader=getattr(yaml,'CSafeLoader',yaml.SafeLoader))
    if not isinstance(value,dict):raise LibraryError('Expected a classified source mapping')
    return value


def records(source: Path, catalogue: dict[str,list[dict[str,Any]]], *, origins: dict[str,str]) -> dict[str,list[dict[str,Any]]]:
    """Prepare rows outside transactions. No effect or runtime adapter is invoked."""
    result: dict[str,list[dict[str,Any]]]={name:[] for name in (
        'source_registry','source_inputs','stage_tasks','stage_selection','stage_discovery',
        'resource_pools','resource_grants','resource_waiters','decoder_owners','decoder_children',
        'dependency_readiness','captured_sources','captured_downloads','captured_download_links',
        'collection_categories','collection_submissions','fiche_progress','collection_checkpoints','checkpoint_members',
        'checkpoint_targets','checkpoint_downloads','checkpoint_links','collection_backfill',
        'backfill_selection','backfill_pages','backfill_items','backfill_page_items',
        'network_budgets','network_starts','routing_packages','retirement_acquisitions',
        'retired_packages','retired_links','retirement_cursor','native_effect','runtime_activation',
        'copy_operations','resolution_previews','manual_proofs','reader_requests','reader_job_state',
        'provider_cache','stage_selection','stage_discovery','native_observation','observed_packages',
        'observed_links','actor_heartbeats','activity_observation','browser_observation','cycle_status',
        'retirement_observation','routing_scheduler','compose_sources','diagnostic_requests')}
    def add(table: str, **row: Any) -> None:result[table].append(row)
    value: Any
    files=source/'files';collector=source/'collector'
    captured=load(collector/'state.yaml') if (collector/'state.yaml').exists() else {}
    monitor=load(collector/'monitor.yaml') if (collector/'monitor.yaml').exists() else {}
    fiches=load(collector/'fiches.yaml') if (collector/'fiches.yaml').exists() else {}
    backfill=load(collector/'backfill.yaml') if (collector/'backfill.yaml').exists() else None
    for value,allowed in ((captured,{'version','items'}),(monitor,{'version','categories','submissions','schedule','collection_checkpoint','source_checkpoint'}),
                          (fiches,{'version','items'})):
        if set(value)-allowed:raise LibraryError('Unclassified collector header field')
    for source_id,value in captured.get('items',{}).items():
        add('captured_sources',source=source_id,payload=encode({**value,**({'downloads':{}} if 'downloads' in value else {})}))
        for target,download in value.get('downloads',{}).items():
            add('captured_downloads',source=source_id,target=target,payload=encode({**download,**({'urls':[]} if 'urls' in download else {})}))
            for position,url in enumerate(download.get('urls',[])):
                add('captured_download_links',source=source_id,target=target,position=position,url=url)
    for key,table in (('categories','collection_categories'),('submissions','collection_submissions')):
        for identity,value in monitor.get(key,{}).items():add(table,source=identity,payload=encode(value))
    for identity,value in fiches.get('items',{}).items():add('fiche_progress',source=identity,payload=encode(value))
    for kind in ('schedule','collection_checkpoint','source_checkpoint'):
        value=monitor.get(kind)
        if value is None:continue
        metadata=decode(encode(value))
        if kind!='schedule':
            for field in ('visited','items','seen_ids','hosters','scope.categories','scope.hosters'):
                target=metadata.get('scope',{}) if field.startswith('scope.') else metadata
                name=field.split('.')[-1]
                if name not in target or target[name] is None:continue
                if not isinstance(target[name],list):raise LibraryError('Invalid checkpoint sequence')
                for position,item in enumerate(target[name]):add('checkpoint_members',kind=kind,field=field,position=position,payload=encode(item))
                target[name]=[]
        if kind=='source_checkpoint':
            for position,(target,item) in enumerate(metadata.get('plan',{}).get('targets',{}).items()):
                add('checkpoint_targets',kind=kind,target=target,position=position,payload=encode(item))
            if 'targets' in metadata.get('plan',{}):metadata['plan']['targets']={}
            for position,(target,item) in enumerate(metadata.get('downloads',{}).items()):
                add('checkpoint_downloads',kind=kind,target=target,position=position,payload=encode({**item,**({'urls':[]} if 'urls' in item else {})}))
                for index,url in enumerate(item.get('urls',[])):add('checkpoint_links',kind=kind,target=target,position=index,url=url)
            if 'downloads' in metadata:metadata['downloads']={}
        add('collection_checkpoints',kind=kind,payload=encode(metadata))
    if backfill is not None:
        batch=str(backfill.get('id') or digest(backfill))
        add('collection_backfill',batch=batch,payload=encode({k:v for k,v in backfill.items() if k not in ('pages','items')}))
        add('backfill_selection',singleton=1,batch=batch)
        for position,(url,members) in enumerate(backfill.get('pages',{}).items()):
            add('backfill_pages',batch=batch,url=url,position=position)
            for index,member in enumerate(members):add('backfill_page_items',batch=batch,url=url,source=member,position=index)
        for identity,value in backfill.get('items',{}).items():add('backfill_items',batch=batch,source=identity,payload=encode(value))
    for name,table,field,key in (('mirror-routing.yaml','routing_packages','packages','pid'),
                               ('package-retirement.yaml','retirement_acquisitions','acquisitions','source')):
        path=collector/name
        if not path.exists():continue
        value=load(path)
        if set(value)-{'schema',field,'cursor'}:raise LibraryError('Unclassified native history header')
        if table=='retirement_acquisitions':
            from retirement_history import validate_history
            from booksys_core.errors import CollectionError
            try:validate_history(value)
            except CollectionError:raise LibraryError('Invalid frozen retirement history') from None
        for identity,item in value.get(field,{}).items():
            if table=='routing_packages':add(table,pid=str(identity),payload=encode(item))
            else:
                add(table,source=identity,payload=encode({**item,'packages':{}}))
                for pid,package in item.get('packages',{}).items():
                    add('retired_packages',source=identity,pid=str(pid),payload=encode({**package,'links':[]}))
                    for position,link in enumerate(package.get('links',[])):
                        add('retired_links',source=identity,pid=str(pid),position=position,payload=encode(link))
        if table=='retirement_acquisitions' and 'cursor' in value:add('retirement_cursor',singleton=1,source=value['cursor'])
    tasks={row['key']:json.loads(row['payload']) for row in catalogue.get('stage_tasks',[])}
    policy=load(files/'task-policy.yaml') if (files/'task-policy.yaml').exists() else {'sources':{},'tasks':{}}
    if set(policy)-{'schema','sources','tasks'}:raise LibraryError('Unclassified task-policy header')
    tasks.update(policy.get('tasks',{}))
    for key,task in tasks.items():
        checked=Task(**task)
        if checked.key!=key:raise LibraryError('Task policy identity differs')
        add('stage_tasks',key=key,stage=task['stage'],status=task.get('status','pending'),age=task['first_eligible_at'],
            due=task['due_at'],blocked=task.get('blocked',''),payload=json.dumps(task,sort_keys=True,separators=(',',':'),ensure_ascii=False))
    for identity,value in policy.get('sources',{}).items():
        registration={k:v for k,v in value.items() if k!='inputs'}
        if value.get('native'):registration['native']={k:v for k,v in value['native'].items() if k not in ('inputs','guards','bindings')}
        registration.setdefault('kind','backfill' if Path(value['path']).name=='backfill.yaml' else 'monitor')
        add('source_registry',id=identity,payload=encode(registration))
        queue=value['queue'];native=value.get('native',{})
        from library.native_projection import project
        view=(project(queue,backfill,auxiliary=fiches) if registration['kind']=='backfill' and backfill is not None else
              project(queue,monitor,auxiliary=fiches)) if native else {}
        for entity,revision in value.get('inputs',{}).items():
            local=entity.removeprefix(identity+':')
            candidates=[task for task in tasks.values() if task.get('source')==identity and task['entity']==entity and task['input_revision']==revision]
            methods={task['method'] for task in candidates}
            if len(methods)>1:raise LibraryError('Source input methods conflict')
            method=next(iter(methods)) if methods else view.get('items',{}).get(local,{}).get('method')
            if method is None:raise LibraryError('Source input has no retained method')
            guard_value=native.get('guards',{}).get(local,{})
            binding=view.get('_native',{}).get('bindings',{}).get(local)
            # A source can be stale. Bind its original domain identity without
            # granting a fresh attempt or changing retained input/allowance.
            if binding is None and native:
                matches=[url for url in monitor.get('submissions',{}) if local in ('source-'+digest(url),'fiche-'+digest(url))]
                if len(matches)==1:binding={'kind':'fiche' if local.startswith('fiche-') else 'submission','identity':matches[0]}
                elif local=='backfill' and backfill is not None:binding={'kind':'backfill','identity':str(backfill.get('id') or digest(backfill)),'original':revision}
                else:raise LibraryError('Source input identity is unclassified')
            if binding is not None and binding['kind']=='backfill':
                if backfill is None:raise LibraryError('Backfill binding has no source')
                binding={**binding,'identity':str(backfill.get('id') or digest(backfill))}
            if binding is not None:binding={**binding,'registration_kind':registration['kind'],'projection_guard':guard_value}
            add('source_inputs',source=identity,entity=entity,input_revision=revision,method=method,guard=digest(guard_value),progress=None,binding=encode(binding) if binding is not None else None)
    for path in sorted(files.glob('stage-selection-*.yaml')):
        value=load(path);after=value.get('after')
        add('stage_selection',queue=path.name[len('stage-selection-'):-len('.yaml')],after_age=after[0] if after else None,
            after_key=after[1] if after else None,lookback_done=value.get('lookback_done',False))
    for name,kind in (('stage-discovery.yaml','business'),('stage-intake-discovery.yaml','intake')):
        if (files/name).exists():add('stage_discovery',kind=kind,after_key=load(files/name).get('after',''))
    if (files/'stage-resources.yaml').exists():
        value=load(files/'stage-resources.yaml')
        if set(value)-{'schema','capacity','reserve_bytes','external_policy','grants','waiters'}:raise LibraryError('Unclassified resource policy')
        add('resource_pools',pool='stage-resources.yaml',capacity=json.dumps(value['capacity']),reserve_bytes=value['reserve_bytes'],policy=value['external_policy'])
        for name in ('grants','waiters'):
            for key,item in value.get(name,{}).items():
                if set(item)-{'age','queue','demand'}:raise LibraryError('Unclassified resource owner')
                add('resource_'+name,pool='stage-resources.yaml',key=key,age=item['age'],queue=item.get('queue',''),demand=json.dumps(item['demand']))
    for path in sorted((files/'stage-children').glob('*.yaml')):
        value=load(path)
        if set(value)-{'schema','names'}:raise LibraryError('Unclassified child owner')
        add('decoder_owners',owner=path.name)
        for position,name in enumerate(value['names']):add('decoder_children',owner=path.name,name=name,position=position+1,stopped=False)
    if (files/'reader-dependency-children.yaml').exists():
        value=load(files/'reader-dependency-children.yaml')
        if set(value)-{'schema','names'}:raise LibraryError('Unclassified dependency children')
        add('decoder_owners',owner='reader-dependency-children.yaml')
        for position,name in enumerate(value['names']):add('decoder_children',owner='reader-dependency-children.yaml',name=name,position=position+1,stopped=False)
    if (files/'dependency-readiness.yaml').exists():
        value=load(files/'dependency-readiness.yaml')
        for name,item in value.get('dependencies',{}).items():
            if set(item)-{'available','at','recovered','url','next_probe'}:raise LibraryError('Unclassified readiness field')
            add('dependency_readiness',name=name,available=item['available'],at=float(item['at']),recovered=float(item.get('recovered',0)),url=item.get('url',''),next_probe=float(item.get('next_probe',0)))
    for name in ('provider-admission.yaml','native-admission.yaml'):
        if not (files/name).exists():continue
        value=load(files/name)
        if set(value)-{'schema','requests','period','spacing','review_block','last','not_before','starts'}:raise LibraryError('Unclassified network admission field')
        add('network_budgets',scope=name,requests=value['requests'],period=float(value['period']),spacing=float(value['spacing']),
            review_block=value['review_block'],last=float(value['last']),not_before=float(value['not_before']))
        for position,stamp in enumerate(value.get('starts',[])):add('network_starts',scope=name,position=position+1,at=float(stamp))
    if (files/'native-effects.yaml').exists():add('native_effect',singleton=1,payload=encode(load(files/'native-effects.yaml')))
    for name,role in (('stage-activation.yaml','stage'),('library-activation.yaml','library')):
        if (files/name).exists():add('runtime_activation',role=role,payload=encode(load(files/name)))
    for name,namespace in (('library-operations','publication'),('library-admission-operations','admission'),('library-provisional-operations','provisional')):
        for path in sorted((files/name).glob('*.yaml')):
            value=load(path)
            add('copy_operations',namespace=namespace,id=str(value.get('id',path.stem)),payload=encode(value))
    for path in sorted((files/'library-resolution'/'previews').glob('*.yaml')):
        value=load(path);add('resolution_previews',asset=value['asset'],id=value['id'],payload=encode(value))
    for path in sorted((files/'proof-requests').glob('*.yaml')):
        if path.name.endswith('.result.yaml'):continue
        value=load(path);output=path.with_name(path.stem+'.result.yaml')
        add('manual_proofs',reference=path.stem,request=encode(value),result=encode(load(output)) if output.exists() else None)
    # Less common source families are handled explicitly below. A present shape
    # that has no classified adapter must block instead of silently becoming proof.
    if (files/'library-retirement.yaml').exists():raise LibraryError('Present historical source shape requires classification')
    if (files/'library-network-budget.yaml').exists():
        value=load(files/'library-network-budget.yaml')
        if set(value)-{'remaining','last_request','cycle_started'} or not (files/'provider-admission.yaml').exists():
            raise LibraryError('Active legacy network allowance requires classification')
    for namespace in ('provider-cache','library-provider-cache'):
        paths=sorted((files/namespace).glob('*.yaml'))
        if paths and not origins.get('library'):raise LibraryError('Provider cache origin is required')
        for path in paths:
            value=load(path);url=value.get('url')
            if not isinstance(url,str) or path.stem!=digest(url):raise LibraryError('Provider cache URL binding differs')
            add('provider_cache',namespace=str(Path(origins['library'])/namespace),url=url,payload=encode(value))
    for path in sorted((files/'reader-requests').glob('*/manifest.json')):
        job=path.parent.name;raw=path.read_text();manifest=json.loads(raw)
        grant=json.loads((path.parent/'grant.json').read_text())
        if set(grant)!={'key'} or not isinstance(manifest,dict):raise LibraryError('Reader request binding differs')
        add('reader_requests',job=job,grant_key=grant['key'],operation=manifest['operation'],manifest=encode(manifest),manifest_raw=raw)
    requests={row['job'] for row in result['reader_requests']}
    for job_path in sorted((files/'reader-jobs').iterdir()) if (files/'reader-jobs').exists() else []:
        if not job_path.is_dir():raise LibraryError('Reader job root contains an unknown file')
        if job_path.name not in requests:raise LibraryError('Reader phase has no retained request')
        if not origins.get('reader_jobs'):raise LibraryError('Reader job origin is required')
        for phase in ('intent','container','receipt','reconciled','cleanup'):
            path=job_path/(phase+'.yaml')
            if not path.exists():continue
            value=load(path)
            import hashlib
            proof={'path':str(Path(origins['reader_jobs'])/job_path.name/path.name),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            add('reader_job_state',job=job_path.name,kind=phase,payload=encode({**value,'_evidence':proof}))
    if (files/'compose-diagnostic-sources.yaml').exists():
        value=load(files/'compose-diagnostic-sources.yaml')
        for role,generation in value.get('sources',{}).items():
            add('compose_sources',role=role,observed_at=float(value['observed_at']),generation=generation)
    for folder in (files/'stage-actors',files/'evidence'/'stage-actors'):
        for path in sorted(folder.glob('*.yaml')):
            value=load(path)
            if set(value)!={'observed_at'}:raise LibraryError('Unclassified actor observation')
            add('actor_heartbeats',queue=path.stem,observed_at=float(value['observed_at']))
    for name,table in (('activity-dashboard.yaml','activity_observation'),('browser-session.yaml','browser_observation'),
                       ('cycle.yaml','cycle_status'),('retirement-cycle.yaml','retirement_observation'),
                       ('mirror-routing-cycle.yaml','routing_scheduler')):
        if (files/'evidence'/name).exists():add(table,singleton=1,payload=encode(load(files/'evidence'/name)))
    queue=files/'snapshots'/'queue.yaml';capacity=files/'snapshots'/'external-capacity.yaml'
    if queue.exists() or capacity.exists():
        if not queue.exists() or not capacity.exists():raise LibraryError('Native observation lacks its freshness binding')
        packages=yaml.load(queue.read_bytes(),Loader=getattr(yaml,'CSafeLoader',yaml.SafeLoader))
        if not isinstance(packages,list):raise LibraryError('Invalid retained native observation')
        value=load(capacity)
        add('native_observation',singleton=1,observed_at=float(value['observed_at']),remaining_bytes=value['remaining_bytes'],overshoot_bytes=value['overshoot_bytes'])
        for position,package in enumerate(packages):
            add('observed_packages',pid=package['pid'],position=position,payload=encode({k:v for k,v in package.items() if k!='links'}))
            for index,link in enumerate(package.get('links',[])):add('observed_links',pid=package['pid'],position=index,payload=encode(link))
    return result
