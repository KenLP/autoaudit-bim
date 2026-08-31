# Run trace: run-e92510d2

Generated: 2026-08-27 13:49:24
Mode: `run-revit`
Project: `demo-villa-simulated`
Duration: 0.14s
Events captured: 76

## Pre-loop / global

### Phase: run_recorder

- `run_recorder.started` -- run_id='run-e92510d2', mode='run-revit', folder='\runs\\run-e92510d2'

### Phase: query_specs

- `query_specs.derived` -- backend='revit', scenario='demo_villa', spec_count=2, categories=['Doors', 'Rooms'], follow_host_for=['Doors'], dropped=[]

### Phase: param_catalog

- `param_catalog.loaded` -- path='\config\\param_catalog.2027.yaml', revit_version='2027', categories=18, params=302

### Phase: graph

- `graph.phase2_chain` -- chain='qc → route', diagnostic_on_exit=False

### Phase: revit_query

- `revit_query.category_done` -- category_label='Doors', backend_category='OST_Doors', listing_size=12, hydrated=12, failed=0, follow_host=True, elapsed_ms=0.4
- `revit_query.category_done` -- category_label='Rooms', backend_category='OST_Rooms', listing_size=8, hydrated=8, failed=0, follow_host=False, elapsed_ms=0.1
- `revit_query.done` -- count=20, per_category={'Doors': 12, 'Rooms': 8}, type_cache_size=5, instance_cache_size=21, elapsed_ms=0.6, elements_per_sec=33333.3, type_cache_hits=19, type_cache_misses=5, type_hit_ratio=0.792, instance_cache_hits=11, instance_hit_ratio=0.344
- `revit_query.category_done` -- category_label='Doors', backend_category='OST_Doors', listing_size=12, hydrated=12, failed=0, follow_host=True, elapsed_ms=0.3
- `revit_query.category_done` -- category_label='Rooms', backend_category='OST_Rooms', listing_size=8, hydrated=8, failed=0, follow_host=False, elapsed_ms=0.1
- `revit_query.done` -- count=20, per_category={'Doors': 12, 'Rooms': 8}, type_cache_size=5, instance_cache_size=21, elapsed_ms=0.5, elements_per_sec=40000.0, type_cache_hits=19, type_cache_misses=5, type_hit_ratio=0.792, instance_cache_hits=11, instance_hit_ratio=0.344
- `revit_query.category_done` -- category_label='Doors', backend_category='OST_Doors', listing_size=12, hydrated=12, failed=0, follow_host=True, elapsed_ms=0.2
- `revit_query.category_done` -- category_label='Rooms', backend_category='OST_Rooms', listing_size=8, hydrated=8, failed=0, follow_host=False, elapsed_ms=0.1
- `revit_query.done` -- count=20, per_category={'Doors': 12, 'Rooms': 8}, type_cache_size=5, instance_cache_size=21, elapsed_ms=0.5, elements_per_sec=40000.0, type_cache_hits=19, type_cache_misses=5, type_hit_ratio=0.792, instance_cache_hits=11, instance_hit_ratio=0.344

### Phase: QC

- `qc_agent.done` -- findings=6, manual_review=0, missing_data=3, compliant=43, total=52, trace_records=52, high=3, medium=2, low=1
- `qc_agent.done` -- findings=5, manual_review=0, missing_data=2, compliant=45, total=52, trace_records=52, high=3, medium=2, low=0
- `qc_agent.done` -- findings=5, manual_review=0, missing_data=2, compliant=45, total=52, trace_records=52, high=3, medium=2, low=0

### Phase: Routing

- `route.first_iteration` -- findings=6
- `route.continue` -- findings=5, previous=6, delta=1, fingerprint_changed=True
- `route.converged` -- reason='fingerprint_unchanged', current=5, previous=5

### Phase: Design

- `design_agent.start` -- total_findings=6, missing_data_items=3, missing_to_path_b=3, missing_to_path_a=0, candidates_after_filter=7, path_a_full=1, path_b_full=6, path_a_selected=1, path_b_selected=6, rule_filter=None, dry_run_only=False, path_b_available=True
- `design_agent.partition` -- path_a=1, path_b=6
- `design_agent.revit.preview` -- rule='demo.doors.fire_rating', element_id='703', parameter='Fire Rating', new_value='2 HR', changes={'before': '120 min', 'after': '2 HR'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_high', deterministic=False, autofill_strategy='inherit_then_normalize', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.doors.mark_naming', element_id='705', parameter='Mark', new_value='D_105', changes={'before': 'D 105', 'after': 'D_105'}
- `design_agent.revit.autonomy` -- decision='auto', severity='severity_low', deterministic=False, autofill_strategy='normalize', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.siblings_cached` -- parameter='Number', category='Rooms', count=4, source='rooms'
- `design_agent.revit.preview` -- rule='demo.rooms.number_unique', element_id='402', parameter='Number', new_value='101A', changes={'before': '101', 'after': '101A'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_medium', deterministic=False, autofill_strategy='none', value_strategy='next_available', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.rooms.number_unique', element_id='403', parameter='Number', new_value='101B', changes={'before': '101', 'after': '101B'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_medium', deterministic=False, autofill_strategy='none', value_strategy='next_available', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.doors.fire_rating', element_id='701', parameter='Fire Rating', new_value='2 HR', changes={'before': '', 'after': '2 HR'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_high', deterministic=False, autofill_strategy='inherit_then_normalize', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.rooms.department_required', element_id='401', parameter='Department', new_value='General', changes={'before': None, 'after': 'General'}
- `design_agent.revit.autonomy` -- decision='auto', severity='severity_low', deterministic=True, autofill_strategy='compose_template', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.revit.batch_executed` -- writes=2, steps=2, committed=2
- `design_agent.subtype_discovered` -- subtype_id='subtype-quality', subtype_title='Quality', type_title='Quality', active_count=2, total_count=4
- `design_agent.path_a_grouped` -- groups=1, kept=1, dropped=0, geometry_findings=0
- `design_agent.rule_group_preview` -- rule='demo.doors.width_min', bucket='non_compliant', elements=1, severity='severity_high', decision='auto'
- `design_agent.rule_group_executed` -- issue_id='issue-mock-0001', display_id=1001, rule='demo.doors.width_min', elements=1
- `design_agent.done` -- proposed=7, executed=3
- `design_agent.start` -- total_findings=5, missing_data_items=2, missing_to_path_b=2, missing_to_path_a=0, candidates_after_filter=5, path_a_full=1, path_b_full=4, path_a_selected=1, path_b_selected=4, rule_filter=None, dry_run_only=False, path_b_available=True
- `design_agent.partition` -- path_a=1, path_b=4
- `design_agent.revit.preview` -- rule='demo.doors.fire_rating', element_id='703', parameter='Fire Rating', new_value='2 HR', changes={'before': '120 min', 'after': '2 HR'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_high', deterministic=False, autofill_strategy='inherit_then_normalize', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.siblings_cached` -- parameter='Number', category='Rooms', count=4, source='rooms'
- `design_agent.revit.preview` -- rule='demo.rooms.number_unique', element_id='402', parameter='Number', new_value='101A', changes={'before': '101', 'after': '101A'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_medium', deterministic=False, autofill_strategy='none', value_strategy='next_available', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.rooms.number_unique', element_id='403', parameter='Number', new_value='101B', changes={'before': '101', 'after': '101B'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_medium', deterministic=False, autofill_strategy='none', value_strategy='next_available', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.revit.preview` -- rule='demo.doors.fire_rating', element_id='701', parameter='Fire Rating', new_value='2 HR', changes={'before': '', 'after': '2 HR'}
- `design_agent.revit.autonomy` -- decision='approve', severity='severity_high', deterministic=False, autofill_strategy='inherit_then_normalize', value_strategy='inferred', llm_proposed=False, propose_only=False
- `design_agent.revit.skipped_execute` -- reason='autonomy=approve'
- `design_agent.path_a_grouped` -- groups=1, kept=1, dropped=0, geometry_findings=0
- `design_agent.done` -- proposed=12, executed=3

### Phase: design

- `design.proposal_already_parked` -- rule_id='demo.doors.fire_rating', fingerprint='127cc181', issue_id='issue-mock-0001'
- `design.proposal_already_parked` -- rule_id='demo.rooms.number_unique', fingerprint='2334aa6f', issue_id='issue-mock-0002'
- `design.proposal_skipped_duplicate` -- rule_id='demo.doors.fire_rating', fingerprint='127cc181', issue_id='issue-mock-0001', note='same rule+write-set already proposed this run'
- `design.proposal_skipped_duplicate` -- rule_id='demo.rooms.number_unique', fingerprint='2334aa6f', issue_id='issue-mock-0002', note='same rule+write-set already proposed this run'
- `design.issue_skipped_duplicate` -- rule_id='demo.doors.width_min', bucket='non_compliant', elements=1, note='same rule+bucket+element-set already proposed this run'

### Phase: Checkpoint

- `checkpoint.written` -- path='\checkpoints\\20260827\\iterat...', keys=['project_id', 'iteration', 'max_iterations', 'findings', 'proposed_fixes', 'outcomes_summary', 'manual_review_items', 'missing_data_items', 'geometry_findings', 'fix_write_log', 'query_coverage', 'status', 'error']
- `checkpoint.written` -- path='\checkpoints\\20260827\\iterat...', keys=['project_id', 'iteration', 'max_iterations', 'findings', 'proposed_fixes', 'prev_finding_count', 'prev_findings_fingerprint', 'outcomes_summary', 'manual_review_items', 'missing_data_items', 'geometry_findings', 'iteration_history', 'fix_write_log', 'query_coverage', 'status', 'error']

## Iteration 0

### Phase: revit_query

- `revit_query.start` -- categories=['Doors', 'Rooms'], backend_categories=['OST_Doors', 'OST_Rooms'], follow_host_for=['Doors'], fetch_concurrency=4

### Phase: QC

- `qc_agent.start` -- element_count=20, rule_count=5

## Iteration 1

### Phase: Iteration bump

- `bump.next_iteration` -- prev_finding_count=6, prev_fingerprint_size=6

### Phase: revit_query

- `revit_query.start` -- categories=['Doors', 'Rooms'], backend_categories=['OST_Doors', 'OST_Rooms'], follow_host_for=['Doors'], fetch_concurrency=4

### Phase: QC

- `qc_agent.start` -- element_count=20, rule_count=5

## Iteration 2

### Phase: Iteration bump

- `bump.next_iteration` -- prev_finding_count=5, prev_fingerprint_size=5

### Phase: revit_query

- `revit_query.start` -- categories=['Doors', 'Rooms'], backend_categories=['OST_Doors', 'OST_Rooms'], follow_host_for=['Doors'], fetch_concurrency=4

### Phase: QC

- `qc_agent.start` -- element_count=20, rule_count=5
