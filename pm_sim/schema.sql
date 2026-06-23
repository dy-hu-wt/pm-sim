PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sim_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  goals TEXT NOT NULL DEFAULT '[]',
  constraints_json TEXT NOT NULL DEFAULT '{}',
  availability_json TEXT NOT NULL DEFAULT '{}',
  private_knowledge_json TEXT NOT NULL DEFAULT '{}',
  behavior_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS coworker_state (
  person_id TEXT NOT NULL REFERENCES people(id),
  key TEXT NOT NULL,
  value_json TEXT NOT NULL DEFAULT 'null',
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'seed',
  PRIMARY KEY (person_id, key)
);

CREATE INDEX IF NOT EXISTS idx_coworker_state_person
  ON coworker_state(person_id, key);

CREATE TABLE IF NOT EXISTS actor_goals (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES people(id),
  project_id TEXT REFERENCES projects(id),
  description TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_actor_goals_person
  ON actor_goals(person_id, status, priority);

CREATE TABLE IF NOT EXISTS actor_workload (
  person_id TEXT PRIMARY KEY REFERENCES people(id),
  current_focus TEXT NOT NULL DEFAULT '',
  capacity_minutes_remaining INTEGER NOT NULL DEFAULT 0,
  load_level TEXT NOT NULL DEFAULT 'normal',
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS actor_commitments (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES people(id),
  project_id TEXT REFERENCES projects(id),
  commitment_type TEXT NOT NULL,
  description TEXT NOT NULL,
  due_at TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_actor_commitments_person
  ON actor_commitments(person_id, status, due_at);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  risk_level TEXT NOT NULL DEFAULT 'unknown',
  stakeholder_pressure TEXT NOT NULL DEFAULT '',
  deadline TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS pressures (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  owner_id TEXT REFERENCES people(id),
  kind TEXT NOT NULL,
  intensity INTEGER NOT NULL CHECK (intensity BETWEEN 1 AND 10),
  min_intensity INTEGER NOT NULL DEFAULT 1 CHECK (min_intensity BETWEEN 1 AND 10),
  max_intensity INTEGER NOT NULL DEFAULT 10 CHECK (max_intensity BETWEEN 1 AND 10),
  reason TEXT NOT NULL DEFAULT '',
  due_at TEXT,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  CHECK (min_intensity <= max_intensity),
  CHECK (intensity >= min_intensity AND intensity <= max_intensity)
);

CREATE INDEX IF NOT EXISTS idx_pressures_project
  ON pressures(project_id, intensity DESC, due_at, id);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  visibility_scope TEXT NOT NULL,
  owner_id TEXT REFERENCES people(id),
  summary TEXT NOT NULL,
  visible_at TEXT,
  source TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  owner_id TEXT REFERENCES people(id),
  status TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'medium',
  due_at TEXT,
  blocked_by TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dependencies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  upstream_task_id TEXT NOT NULL REFERENCES tasks(id),
  downstream_task_id TEXT NOT NULL REFERENCES tasks(id),
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS blockers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  owner_id TEXT REFERENCES people(id),
  visibility_scope TEXT NOT NULL,
  visible_at TEXT,
  resolved_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  recipient_id TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS calendar_events (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  attendees_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'scheduled',
  transcript_doc_id TEXT REFERENCES docs(id),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS docs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  visibility_scope TEXT NOT NULL,
  visible_at TEXT,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS doc_revisions (
  id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES docs(id),
  actor TEXT NOT NULL,
  previous_body TEXT NOT NULL,
  new_body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_doc_revisions_doc_id
  ON doc_revisions(doc_id, created_at);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_due
  ON events(status, scheduled_at, priority, id);

CREATE TABLE IF NOT EXISTS action_log (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_action_log_created_at
  ON action_log(created_at, id);

CREATE TABLE IF NOT EXISTS action_evidence (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL,
  action_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_action_evidence_key
  ON action_evidence(key, status, created_at);

CREATE TABLE IF NOT EXISTS milestones (
  id TEXT PRIMARY KEY,
  milestone_id TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_milestones_id
  ON milestones(milestone_id, created_at);
