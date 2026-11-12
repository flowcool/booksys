UPDATE work_items SET state = 'waiting'
WHERE state = 'blocked' AND wait_reason = 'effect_uncertain';
