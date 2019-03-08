from .base import BaseWorker
from syft.codes import MSGTYPE
import syft as sy
import random


class PlanPointer:
    def __init__(self, id, location, id_at_location, owner):
        self.id = id
        self.location = location
        self.id_at_location = id_at_location
        self.owner = owner


def replace_ids(obj, change_id, to_id, from_worker, to_worker):
    _obj = list()

    for i, item in enumerate(obj):
        if isinstance(item, int) and (item == change_id):
            _obj.append(to_id)

        elif isinstance(item, type(from_worker)) and (item == from_worker):
            _obj.append(to_worker)

        elif isinstance(item, (list, tuple)):
            _obj.append(
                replace_ids(
                    obj=item,
                    change_id=change_id,
                    to_id=to_id,
                    from_worker=from_worker,
                    to_worker=to_worker,
                )
            )

        else:
            _obj.append(item)

    return _obj


class Plan(BaseWorker):
    """This worker does not send messages or execute any commands. Instead,
    it simply records messages that are sent to it such that message batches
    (called 'Plans') can be created and sent once."""

    def __init__(self, hook, owner, name="", *args, **kwargs):
        super().__init__(hook=hook, *args, **kwargs)
        # Plan instance info
        self.name = name
        self.owner = owner
        # Info about the plan stored
        self.plan = list()
        self.readable_plan = list()
        self.arg_ids = list()
        self.result_ids = list()
        # Pointing info towards a remote plan
        self.location = None
        self.ptr_plan = None

    def _send_msg(self, message, location):
        return location._recv_msg(message)

    def _recv_msg(self, bin_message):
        """
        Upon reception, the PlanWorker store in the plan all commands which can be
        executed lazily
        :param bin_message: the message of a command received
        :return: the None message serialized to specify the command was received
        """
        (some_type, (msg_type, contents)) = sy.serde.deserialize(bin_message, detail=False)

        if msg_type != MSGTYPE.OBJ:
            self.plan.append(bin_message)
            self.readable_plan.append((some_type, (msg_type, contents)))

        # we can't receive the results of a plan without
        # executing it. So, execute the plan.
        if msg_type in (MSGTYPE.OBJ_REQ, MSGTYPE.IS_NONE, MSGTYPE.GET_SHAPE):
            return self.execute_plan()

        return sy.serde.serialize(None)

    def build_plan(self, args):
        """
        The plan must be built with some input data, here args. When they
        are provided, they are sent to the plan worker, which executes its
        blueprint: each command of the blueprint is catched by _recv_msg
        and is used to fill the plan
        :param args: the input data
        """
        print("build plan")
        # The ids of args of the first call, which should be updated when
        # the function is called with new args
        self.arg_ids = list()

        local_args = list()
        for i, arg in enumerate(args):
            self.owner.register_obj(arg)
            arg = arg.send(self)
            arg.child.garbage_collect_data = False
            self.arg_ids.append(arg.id_at_location)
            local_args.append(arg)

        res_ptr = self.plan_blueprint(*local_args)
        res_ptr.child.garbage_collect_data = False

        # The id where the result should be stored
        self.result_ids = [res_ptr.id_at_location]

    def replace_ids(self, from_ids, to_ids):
        """
        Replace pairs of tensor ids in the plan stored
        :param from_ids: the left part of the pair: ids to change
        :param to_ids: the right part of the pair: ids to replace with
        """
        # for every pair of id
        for i in range(len(from_ids)):
            # for every message of the plan
            for j, msg in enumerate(self.readable_plan):
                # look for the old id and replace it with the new one
                self.readable_plan[j] = replace_ids(
                    obj=msg,
                    change_id=from_ids[i],
                    to_id=to_ids[i],
                    from_worker=self.id,
                    to_worker=self.owner.id,
                )
        return self

    def replace_worker_ids(self, from_worker_id, to_worker_id):
        """
        Replace occurrences of from_worker_id by to_worker_id in the plan stored
        """
        self.readable_plan = replace_ids(
            obj=self.readable_plan,
            change_id=-1,
            to_id=-1,
            from_worker=from_worker_id,
            to_worker=to_worker_id,
        )

    def __call__(self, *args, **kwargs):
        """
        Call a plan execution with some arguments, and specify the ids where the result
        should be stored
        :return: The pointer to the result of the execution if the plan was already sent,
        else the None message serialized.
        """
        assert len(kwargs) == 0, "kwargs not supported for plan"
        result_ids = [random.randint(0, 1e10)]
        return self.execute_plan(args, result_ids)

    def execute_plan(self, args, result_ids):
        """
        Control local or remote plan execution.

        If the plan doesn't have the plan built, first build it using the blueprint.

        Then if it has a remote location, send the plan to the remote location only the
        first time, request a remote plan execution with specific pointers and ids for
        storing the result, and return a pointer to the result of the execution.

        If the plan is local: update the plan with the result_ids and args ids given,
        run the plan and return the None message serialized.
        """
        first_run = self.readable_plan == []
        if first_run:
            self.build_plan(args)

        if self.location:
            if self.ptr_plan is None:
                self.ptr_plan = self._send(self.location)
            response = self.request_execute_plan(result_ids, *args)
            return response

        if not self.location:
            arg_ids = [arg.id for arg in args]
            self.replace_ids(self.arg_ids, arg_ids)
            self.arg_ids = arg_ids

            self.replace_ids(self.result_ids, result_ids)
            self.result_ids = result_ids

            for message in self.readable_plan:
                bin_message = sy.serde.serialize(message, simplified=True)
                self.owner.recv_msg(bin_message)

        return sy.serde.serialize(None)

    def request_execute_plan(self, response_ids, *args):
        """
        Send a request to execute the plan on the remote location
        :param response_ids: where the plan result should be stored remotely
        :param args: the arguments use as input data for the plan
        :return:
        """
        args = [args, response_ids]
        command = ("execute_plan", self.ptr_plan, args)

        response = self.owner.send_command(
            message=command, recipient=self.location, return_ids=response_ids
        )
        return response

    def create_pointer(
        self,
        location: BaseWorker = None,
        id_at_location: (str or int) = None,
        register: bool = False,
        owner: BaseWorker = None,
        ptr_id: (str or int) = None,
    ) -> PlanPointer:
        return PlanPointer(ptr_id, location, id_at_location, owner)

    def send(self, location):
        """
        Mock send function that only specify that the Plan will have to be sent to location
        """
        if self.location is not None:
            raise NotImplementedError(
                "Can't send a Plan which already has a location, use .get() before"
            )
        else:
            self.location = location
        return self

    def get(self):
        """
        Mock get function
        """
        self.replace_worker_ids(self.location.id, self.owner.id)
        self.location = None
        self.ptr_plan = None
        return self

    def _send(self, location):
        """
        Real send function that sends the Plan instance with its plan to location
        """
        self.replace_worker_ids(self.owner.id, self.location.id)
        return self.owner.send(obj=self, workers=location)

    def __str__(self):
        """Returns the string representation of PlanWorker.

        Note:
            __repr__ calls this method by default.
        """

        out = "<"
        out += str(type(self)).split("'")[1].split(".")[-1]
        out += " " + str(self.name)
        out += " id:" + str(self.id)
        out += " owner:" + str(self.owner.id)
        if self.location:
            out += " location:" + str(self.location.id)
        if len(self.readable_plan) > 0:
            out += " built"
        out += ">"
        return out