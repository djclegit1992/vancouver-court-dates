-- Vancouver Court Dates Finder
-- Confirmation trigger.
--
-- Same pattern as Manitoba: pg_net posts the inserted row to an Edge
-- Function, wrapped as {"record": {...}}, with a shared secret in an
-- x-webhook-secret header.
--
-- Section 1 is not optional. court_alerts_confirm currently has no
-- WHEN clause, so it fires on every insert. Adding a BC trigger beside
-- it without scoping it would make each BC signup fire both, sending
-- two confirmations, one of them Winnipeg copy from the Winnipeg
-- Postmark server.

-- ===================================================================
-- 1. Scope the Manitoba trigger to Manitoba.
--    The function body is untouched. Only the trigger gains a
--    condition, so MB behaviour is identical and it stops firing on
--    anything else.
-- ===================================================================

drop trigger if exists court_alerts_confirm on public.court_alerts;

create trigger court_alerts_confirm
after insert on public.court_alerts
for each row
when (new.jurisdiction = 'MB')
execute function court_alerts_notify();


-- ===================================================================
-- 2. The BC notify function.
--    Replace REPLACE_WITH_WEBHOOK_SECRET_BC with the value set in
--    Edge Function secrets as WEBHOOK_SECRET_BC.
-- ===================================================================

create or replace function public.court_alerts_notify_bc()
returns trigger
language plpgsql
security definer
set search_path to 'public', 'net'
as $function$
declare
  secret text := 'REPLACE_WITH_WEBHOOK_SECRET_BC';
  fn_url text := 'https://jiimeaykdofnldeucmsk.supabase.co/functions/v1/send-confirmation-bc';
begin
  perform net.http_post(
    url     := fn_url,
    body    := jsonb_build_object('record', to_jsonb(new)),
    headers := jsonb_build_object(
                 'Content-Type',     'application/json',
                 'x-webhook-secret', secret
               ),
    timeout_milliseconds := 5000
  );
  return new;
end;
$function$;


-- ===================================================================
-- 3. The BC trigger.
-- ===================================================================

drop trigger if exists court_alerts_confirm_bc on public.court_alerts;

create trigger court_alerts_confirm_bc
after insert on public.court_alerts
for each row
when (new.jurisdiction = 'BC')
execute function court_alerts_notify_bc();


-- ===================================================================
-- 4. Read back. Two triggers, each with its own WHEN clause.
-- ===================================================================

select tgname, pg_get_triggerdef(oid) as definition
from pg_trigger
where tgrelid = 'public.court_alerts'::regclass
  and not tgisinternal
order by tgname;
