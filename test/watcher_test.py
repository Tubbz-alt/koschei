import time
import signal

from mock import Mock, patch
from common import DBTest

from koschei.watcher import Watcher

test_topic = 'org.fedoraproject.test.buildsys'

def generate_state_change(instance='primary', task_id=666, old='OPEN', new='CLOSED'):
    return {'msg':
        {'instance': instance,
         'attribute': 'state',
         'id': task_id,
         'old': old,
         'new': new,
         }}

class WatcherTest(DBTest):
    def test_ignored_topic(self):
        def tail_messages_mock():
            yield ('', '', 'org.fedoraproject.prod.buildsys.task.state.change',
                   generate_state_change())
        with patch('fedmsg.tail_messages', tail_messages_mock):
            Watcher(db=Mock(), koji_sessions={'primary': Mock(), 'secondary': Mock()}).main()

    def test_ignored_instance(self):
        def tail_messages_mock():
            yield ('', '', test_topic,
                   generate_state_change(instance='ppc'))
        with patch('fedmsg.tail_messages', tail_messages_mock):
            Watcher(db=Mock(), koji_sessions={'primary': Mock(), 'secondary': Mock()}).main()

    def test_task_completed(self):
        def tail_messages_mock():
            yield ('', '', test_topic + '.task.state.change',
                   generate_state_change())
        _, build = self.prepare_basic_data()
        backend_mock = Mock()
        with patch('fedmsg.tail_messages', tail_messages_mock):
            Watcher(db=self.s, koji_sessions={'primary': Mock(), 'secondary': Mock()}, backend=backend_mock).main()
            backend_mock.update_build_state.assert_called_once_with(build, 'CLOSED')
