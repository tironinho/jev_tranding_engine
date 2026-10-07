"""Evolution engine tables."""

from alembic import op

from app.db.models import (
    AnomalyRow,
    CodingAgentTaskRow,
    ExperimentArtifactRow,
    ExperimentHypothesisRow,
    ExperimentMetricRow,
    ExperimentRow,
    ExperimentRunRow,
    PromotionEventRow,
    ResearchReportRow,
    StrategyDecayRow,
    StrategyVersionRow,
)

revision = "20261007_0002"
down_revision = "20261007_0001"
branch_labels = None
depends_on = None

NEW_TABLES = [
    StrategyVersionRow.__table__,
    ExperimentRow.__table__,
    ExperimentHypothesisRow.__table__,
    ExperimentMetricRow.__table__,
    ExperimentRunRow.__table__,
    ExperimentArtifactRow.__table__,
    AnomalyRow.__table__,
    ResearchReportRow.__table__,
    PromotionEventRow.__table__,
    StrategyDecayRow.__table__,
    CodingAgentTaskRow.__table__,
]


def upgrade() -> None:
    bind = op.get_bind()
    StrategyVersionRow.metadata.create_all(bind, tables=NEW_TABLES, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(NEW_TABLES):
        table.drop(bind, checkfirst=True)
