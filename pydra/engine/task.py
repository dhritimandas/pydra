# -*- coding: utf-8 -*-
"""task.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1RRV1gHbGJs49qQB1q1d5tQEycVRtuhw6

## Notes:

### Environment specs
1. neurodocker json
2. singularity file+hash
3. docker hash
4. conda env
5. niceman config
6. environment variables

### Monitors/Audit
1. internal monitor
2. external monitor
3. callbacks

### Resuming
1. internal tracking
2. external tracking (DMTCP)

### Provenance
1. Local fragments
2. Remote server

### Isolation
1. Working directory
2. File (copy to local on write)
3. read only file system
"""


import cloudpickle as cp
import dataclasses as dc
import inspect
import typing as ty
from pathlib import Path

from .core import TaskBase
from ..utils.messenger import AuditFlag
from .specs import (
    File,
    BaseSpec,
    SpecInfo,
    ShellSpec,
    ShellOutSpec,
    ContainerSpec,
    DockerSpec,
    SingularitySpec,
)
from .helpers import ensure_list, execute


class FunctionTask(TaskBase):
    def __init__(
        self,
        func: ty.Callable,
        input_spec: ty.Optional[SpecInfo] = None,
        output_spec: ty.Optional[BaseSpec] = None,
        name=None,
        audit_flags: AuditFlag = AuditFlag.NONE,
        messengers=None,
        messenger_args=None,
        cache_dir=None,
        cache_locations=None,
        **kwargs,
    ):

        if input_spec is None:
            input_spec = SpecInfo(
                name="Inputs",
                fields=[
                    (
                        val.name,
                        val.annotation,
                        dc.field(
                            default=val.default,
                            metadata={
                                "help_string": f"{val.name} parameter from {func.__name__}"
                            },
                        ),
                    )
                    if val.default is not inspect.Signature.empty
                    else (
                        val.name,
                        val.annotation,
                        dc.field(metadata={"help_string": val.name}),
                    )
                    for val in inspect.signature(func).parameters.values()
                ]
                + [("_func", str, cp.dumps(func))],
                bases=(BaseSpec,),
            )
        else:
            input_spec.fields.append(("_func", str, cp.dumps(func)))
        self.input_spec = input_spec
        if name is None:
            name = func.__name__
        super(FunctionTask, self).__init__(
            name,
            inputs=kwargs,
            audit_flags=audit_flags,
            messengers=messengers,
            messenger_args=messenger_args,
            cache_dir=cache_dir,
            cache_locations=cache_locations,
        )
        if output_spec is None:
            if "return" not in func.__annotations__:
                output_spec = SpecInfo(
                    name="Output", fields=[("out", ty.Any)], bases=(BaseSpec,)
                )
            else:
                return_info = func.__annotations__["return"]
                if hasattr(return_info, "__name__") and hasattr(
                    return_info, "__annotations__"
                ):
                    output_spec = SpecInfo(
                        name=return_info.__name__,
                        fields=list(return_info.__annotations__.items()),
                        bases=(BaseSpec,),
                    )
                # Objects like int, float, list, tuple, and dict do not have __name__ attribute.
                elif hasattr(return_info, "__annotations__"):
                    output_spec = SpecInfo(
                        name="Output",
                        fields=list(return_info.__annotations__.items()),
                        bases=(BaseSpec,),
                    )
                elif isinstance(return_info, dict):
                    output_spec = SpecInfo(
                        name="Output",
                        fields=list(return_info.items()),
                        bases=(BaseSpec,),
                    )
                else:
                    if not isinstance(return_info, tuple):
                        return_info = (return_info,)
                    output_spec = SpecInfo(
                        name="Output",
                        fields=[
                            ("out{}".format(n + 1), t)
                            for n, t in enumerate(return_info)
                        ],
                        bases=(BaseSpec,),
                    )
        elif "return" in func.__annotations__:
            raise NotImplementedError("Branch not implemented")
        self.output_spec = output_spec

    def _run_task(self):
        inputs = dc.asdict(self.inputs)
        del inputs["_func"]
        self.output_ = None
        output = cp.loads(self.inputs._func)(**inputs)
        if output:
            output_names = [el[0] for el in self.output_spec.fields]
            self.output_ = {}
            if len(output_names) > 1:
                if len(output_names) == len(output):
                    self.output_ = dict(zip(output_names, output))
                else:
                    raise Exception(
                        f"expected {len(self.output_spec.fields)} elements, "
                        f"but {len(output)} were returned"
                    )
            else:  # if only one element in the fields, everything should be returned together
                self.output_[output_names[0]] = output


class ShellCommandTask(TaskBase):
    def __init__(
        self,
        name=None,
        input_spec: ty.Optional[SpecInfo] = None,
        output_spec: ty.Optional[SpecInfo] = None,
        audit_flags: AuditFlag = AuditFlag.NONE,
        messengers=None,
        messenger_args=None,
        cache_dir=None,
        strip=False,
        **kwargs,
    ):
        if input_spec is None:
            input_spec = SpecInfo(name="Inputs", fields=[], bases=(ShellSpec,))
        self.input_spec = input_spec
        if output_spec is None:
            output_spec = SpecInfo(name="Output", fields=[], bases=(ShellOutSpec,))

        self.output_spec = output_spec

        super(ShellCommandTask, self).__init__(
            name=name,
            inputs=kwargs,
            audit_flags=audit_flags,
            messengers=messengers,
            messenger_args=messenger_args,
            cache_dir=cache_dir,
        )
        self.strip = strip

    @property
    def command_args(self):
        pos_args = []  # list for (position, command arg)
        for f in dc.fields(self.inputs):
            if f.name == "executable":
                pos = 0  # executable should be the first el. of the command
            elif f.name == "args":
                pos = -1  # assuming that args is the last el. of the command
            # if inp has position than it should be treated as a part of the command
            # metadata["position"] is the position in the command
            elif "position" in f.metadata:
                pos = f.metadata["position"]
                if not isinstance(pos, int) or pos < 1:
                    raise Exception(
                        f"position should be an integer > 0, but {pos} given"
                    )
            else:
                continue
            cmd_add = []
            if "argstr" in f.metadata:
                cmd_add.append(f.metadata["argstr"])
            value = getattr(self.inputs, f.name)
            if f.type is bool:
                if value is not True:
                    break
            else:
                cmd_add += ensure_list(value)
            if cmd_add is not None:
                pos_args.append((pos, cmd_add))
        # sorting all elements of the command
        pos_args.sort()
        # if args available, they should be moved at the of the list
        if pos_args[0][0] == -1:
            pos_args.append(pos_args.pop(0))
        # dropping the position index
        cmd_args = []
        for el in pos_args:
            cmd_args += el[1]
        return cmd_args

    @command_args.setter
    def command_args(self, args: ty.Dict):
        self.inputs = dc.replace(self.inputs, **args)

    @property
    def cmdline(self):
        return " ".join(self.command_args)

    def _run_task(self,):
        self.output_ = None
        args = self.command_args
        if args:
            keys = ["return_code", "stdout", "stderr"]
            values = execute(args, strip=self.strip)
            self.output_ = dict(zip(keys, values))


class ContainerTask(ShellCommandTask):
    def __init__(
        self,
        name,
        input_spec: ty.Optional[SpecInfo] = None,
        output_spec: ty.Optional[SpecInfo] = None,
        audit_flags: AuditFlag = AuditFlag.NONE,
        messengers=None,
        messenger_args=None,
        cache_dir=None,
        strip=False,
        **kwargs,
    ):

        if input_spec is None:
            input_spec = SpecInfo(name="Inputs", fields=[], bases=(ContainerSpec,))
        super(ContainerTask, self).__init__(
            name=name,
            input_spec=input_spec,
            output_spec=output_spec,
            audit_flags=audit_flags,
            messengers=messengers,
            messenger_args=messenger_args,
            cache_dir=cache_dir,
            strip=strip,
            **kwargs,
        )

    @property
    def cmdline(self):
        return " ".join(self.container_args + self.command_args)

    @property
    def container_args(self):
        if self.inputs.container is None:
            raise AttributeError("Container software is not specified")
        cargs = [self.inputs.container, "run"]
        if self.inputs.container_xargs is not None:
            cargs.extend(self.inputs.container_xargs)
        if self.inputs.image is None:
            raise AttributeError("Container image is not specified")
        cargs.append(self.inputs.image)
        return cargs

    def binds(self, opt, output_cpath="/output_pydra"):
        """Specify mounts to bind from local filesystems to container

        `bindings` are tuples of (local path, container path, bind mode)
        """
        bargs = []
        output_dir_cpath = None
        for binding in self.inputs.bindings:
            if len(binding) == 3:
                lpath, cpath, mode = binding
            elif len(binding) == 2:
                lpath, cpath, mode = binding + ("rw",)
            else:
                raise Exception(
                    f"binding should have length 2, 3, or 4, it has {len(binding)}"
                )
            if str(lpath) == str(self.output_dir):
                output_dir_cpath = cpath
            if mode is None:
                mode = "rw"  # default
            bargs.extend([opt, "{0}:{1}:{2}".format(lpath, cpath, mode)])

        # output_dir is added to the bindings if not part of self.inputs.bindings
        if not output_dir_cpath:
            output_dir_cpath = output_cpath
            bargs.extend(
                [
                    opt,
                    "{0}:{1}:{2}".format(str(self.output_dir), output_dir_cpath, "rw"),
                ]
            )
        # TODO: would need changes for singularity
        bargs.extend(["-w", output_dir_cpath])
        return bargs

    def _run_task(self):
        self.output_ = None
        args = self.container_args + self.command_args
        if args:
            keys = ["return_code", "stdout", "stderr"]
            values = execute(args, strip=self.strip)
            self.output_ = dict(zip(keys, values))


class DockerTask(ContainerTask):
    def __init__(
        self,
        name,
        input_spec: ty.Optional[SpecInfo] = None,
        output_spec: ty.Optional[SpecInfo] = None,
        audit_flags: AuditFlag = AuditFlag.NONE,
        messengers=None,
        messenger_args=None,
        cache_dir=None,
        strip=False,
        **kwargs,
    ):
        if input_spec is None:
            input_spec = SpecInfo(name="Inputs", fields=[], bases=(DockerSpec,))
        super(ContainerTask, self).__init__(
            name=name,
            input_spec=input_spec,
            output_spec=output_spec,
            audit_flags=audit_flags,
            messengers=messengers,
            messenger_args=messenger_args,
            cache_dir=cache_dir,
            strip=strip,
            **kwargs,
        )

    @property
    def container_args(self):
        cargs = super().container_args
        assert self.inputs.container == "docker"
        # insert bindings before image
        idx = len(cargs) - 1
        cargs[idx:-1] = self.binds("-v")
        return cargs


class SingularityTask(ContainerTask):
    def __init__(
        self,
        input_spec: ty.Optional[SpecInfo] = None,
        output_spec: ty.Optional[SpecInfo] = None,
        audit_flags: AuditFlag = AuditFlag.NONE,
        messengers=None,
        messenger_args=None,
        cache_dir=None,
        strip=False,
        **kwargs,
    ):
        if input_spec is None:
            field = dc.field(default_factory=list)
            field.metadata = {}
            fields = [("args", ty.List[str], field)]
            input_spec = SpecInfo(
                name="Inputs", fields=fields, bases=(SingularitySpec,)
            )
        super(ContainerTask, self).__init__(
            input_spec=input_spec,
            audit_flags=audit_flags,
            messengers=messengers,
            messenger_args=messenger_args,
            cache_dir=cache_dir,
            strip=strip,
            **kwargs,
        )

    @property
    def container_args(self):
        cargs = super().container_args
        assert self.inputs.container == "singularity"
        # insert bindings before image
        idx = len(cargs) - 1
        cargs[idx:-1] = self.binds("-B")
        return cargs
