"""Add package state

Revision ID: 44ee2dab26cb
Revises: None
Create Date: 2014-06-26 17:35:19.420281

"""

# revision identifiers, used by Alembic.
revision = '44ee2dab26cb'
down_revision = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('package', sa.Column('state', sa.Integer(), server_default='0', nullable=False))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('package', 'state')
    ### end Alembic commands ###