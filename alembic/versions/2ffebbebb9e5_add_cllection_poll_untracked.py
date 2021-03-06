"""Add cllection.poll_untracked

Revision ID: 2ffebbebb9e5
Revises: 5992b944544b
Create Date: 2016-03-30 08:41:02.756399

"""

# revision identifiers, used by Alembic.
revision = '2ffebbebb9e5'
down_revision = '5992b944544b'

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('collection', sa.Column('poll_untracked', sa.Boolean(), server_default=sa.text(u'true'), nullable=False))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('collection', 'poll_untracked')
    ### end Alembic commands ###
