"""为重放功能引入投递轮次字段

背景：死信重放需要把事件的尝试次数清零，让它重新获得完整的重试预算。
但 delivery_attempts 上有 UNIQUE(event_id, attempt_number)，
清零后新的第 1 次尝试会和历史记录里的第 1 次撞上。

两种看似简单、实则错误的解法：

1. 重放时删掉旧的投递记录 —— 丢掉了排查"当初为什么失败"的唯一依据
2. 重放时不重置次数 —— 事件刚一出队就再次超过 max_attempts，重放没有意义

正确解法是加一列 generation 区分轮次，把唯一约束改成三元组：
同一个事件的同一轮次内，第几次尝试是唯一的。

Revision ID: df544657aef4
Revises: 6dabe5be6253
Create Date: 2026-09-13 08:36:00.897152
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "df544657aef4"
down_revision: str | None = "6dabe5be6253"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 已有数据默认归入第 0 轮，语义上正好对应"首次入队"
    op.add_column(
        "delivery_attempts",
        sa.Column("generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.drop_constraint(op.f("uq_attempts_event_number"), "delivery_attempts", type_="unique")
    op.create_unique_constraint(
        "uq_attempts_event_generation_number",
        "delivery_attempts",
        ["event_id", "generation", "attempt_number"],
    )
    op.add_column(
        "events",
        sa.Column("attempt_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema.

    注意：如果此时库里已经存在重放过的数据（同一事件存在多轮尝试），
    折叠成 (event_id, attempt_number) 会因重复而失败。
    这种情况下需要先人工处理数据，而不是强行降级。
    """
    op.drop_column("events", "attempt_generation")
    op.drop_constraint("uq_attempts_event_generation_number", "delivery_attempts", type_="unique")
    op.create_unique_constraint(
        op.f("uq_attempts_event_number"),
        "delivery_attempts",
        ["event_id", "attempt_number"],
    )
    op.drop_column("delivery_attempts", "generation")
