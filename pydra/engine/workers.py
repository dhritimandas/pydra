import time
import multiprocessing as mp
import asyncio
import sys
import re

# from pycon_utils import make_cluster
from dask.distributed import Client
import concurrent.futures as cf

from .helpers import create_pyscript, execute, save

import logging

logger = logging.getLogger("pydra.worker")


class Worker:
    def __init__(self, loop=None):
        logger.debug("Initialize Worker")
        self.loop = loop

    def run_el(self, interface, **kwargs):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    async def fetch_finished(self, futures):
        raise NotImplementedError


class DistributedWorker(Worker):
    """Base Worker for distributed execution"""

    @staticmethod
    def _prepare_runscripts(task, interpretter):
        pyscript = create_pyscript(
            (task.output_dir / "task.pklz"), task.hash
        )
        batchscript = pyscript.parent / f"batchscript_{task.hash}.sh"
        shebang = f"#!{interpretter}"
        bcmd = "\n".join((shebang, f"{sys.executable} {str(pyscript)}"))
        with batchscript.open('wt') as fp:
            fp.writelines(bcmd)
        return batchscript


class MpWorker(Worker):
    def __init__(self, nr_proc=4):  # should be none
        self.nr_proc = nr_proc
        self.pool = mp.Pool(processes=self.nr_proc)
        logger.debug("Initialize MpWorker")

    def run_el(self, interface, inp):
        x = self.pool.apply_async(interface, inp)
        # returning dir_nm_el and Result object for the specific element
        return x.get()

    def close(self):
        # added this method since I was having somtetimes problem with reading results from (existing) files
        # i thought that pool.close() should work, but still was getting some errors, so testing terminate
        self.pool.terminate()


class SerialPool:
    """ a simply class to imitate a pool like in cf"""

    def submit(self, interface, **kwargs):
        self.res = interface(**kwargs)

    def result(self):
        return self.res

    def done(self):
        return True


class SerialWorker(Worker):
    def __init__(self):
        logger.debug("Initialize SerialWorker")
        self.pool = SerialPool()

    def run_el(self, interface, **kwargs):
        self.pool.submit(interface=interface, **kwargs)
        return self.pool

    def close(self):
        pass


class ConcurrentFuturesWorker(Worker):
    def __init__(self, nr_proc=None):
        super(ConcurrentFuturesWorker, self).__init__()
        self.nr_proc = nr_proc or mp.cpu_count()
        # added cpu_count to verify, remove once confident and let PPE handle
        self.pool = cf.ProcessPoolExecutor(self.nr_proc)
        # self.loop = asyncio.get_event_loop()
        logger.debug("Initialize ConcurrentFuture")

    def run_el(self, interface, **kwargs):
        # wrap as asyncio task
        if not self.loop:
            raise Exception("No event loop available to submit tasks")
        task = asyncio.create_task(
            self.exec_as_coro(interface)
        )
        return task

    async def exec_as_coro(self, interface):  # sidx=None):
        res = await self.loop.run_in_executor(self.pool, interface)
        return res

    def close(self):
        self.pool.shutdown()

    async def fetch_finished(self, futures):
        """Awaits asyncio ``Tasks`` until one is finished

        Parameters
        ----------
        futures : set of ``Futures``
            Pending tasks

        Returns
        -------
        done : set
            Finished or cancelled tasks
        """
        done = set()
        try:
            done, pending = await asyncio.wait(
                futures, return_when=asyncio.FIRST_COMPLETED
            )
        except ValueError:
            # nothing pending!
            pending = set()
        logger.debug(f"Tasks finished: {len(done)}")
        return done, pending


class DaskWorker(Worker):
    def __init__(self):
        logger.debug("Initialize Dask Worker")
        # self.cluster = LocalCluster()
        self.client = Client()  # self.cluster)
        # print("BOKEH", self.client.scheduler_info()["address"] + ":" + str(self.client.scheduler_info()["services"]["bokeh"]))

    def run_el(self, interface, **kwargs):
        print("DASK, run_el: ", interface, kwargs, time.time())
        # dask  doesn't copy the node second time, so it doesn't see that I change input in the meantime (??)
        x = self.client.submit(interface, **kwargs)
        print("DASK, status: ", x.status)
        # this important, otherwise dask will not finish the job
        x.add_done_callback(lambda x: print("DONE ", interface, kwargs))
        print("res", x.result())
        # returning dir_nm_el and Result object for the specific element
        # return x.result()
        return x

    def close(self):
        # self.cluster.close()
        self.client.close()


class SLURMWorker(DistributedWorker):
    _cmd = "sbatch"

    def __init__(self, sbatch_args=None, **kwargs):
        super(SLURMWorker, self).__init__()
        self.sbatch_args = sbatch_args
        self.pending = set()

    def _submit_job(self, batchscript, jobname):
        cmd = f"{self._cmd} {self.sbatch_args or ''} -J {jobname} {batchscript}"
        _, stdout, _ = execute(cmd)
        jobid = re.search(r"\d+", stdout)
        if not jobid:
            raise RuntimeError("Could not extract job ID")
        self.pending.add(jobid)

    def _poll_job(self):
        cmd = f"squeue -j {','.join(self.pending)}"
        _, stdout, _ = execute(cmd)

    def run_el(self, task):
        """
        xx 1) Pickle task
        xx 2) Create python run script
        xx 3) Create bash submission script
        xx 4) Submit with sbatch
        xx 5) Add jobid to pending
        """
        save(task.output_dir, task=task)
        runscript = self._prepare_runscript(task, interpreter="/bin/bash")
        jobname = ".".join((task.name, task.hash))
        self._submit_job(runscript, jobname)

    def close(self):
        pass

    async def fetch_finished(self, jobs):
        """
        Waits until at least one job finishes
        """
        pass