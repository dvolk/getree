import os
import sys
import tempfile
import uuid
import json
import sqlite3
import subprocess
import threading
import time
import yaml
import functools
import pathlib
import tempfile
from io import StringIO
import collections

from matplotlib.backends.backend_svg import FigureCanvasSVG as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from flask import Flask, request, render_template, make_response, redirect, abort
from flasgger import Swagger

from config import cfg
import lib

con = sqlite3.connect(cfg['sqlitedbfilepath'], check_same_thread=False)
db_lock = threading.Lock()

with db_lock, con:
    con.execute("delete from queue")

guid_tree_map = collections.defaultdict(list)

def make_guid_tree_map():
    for sample_guids, tree in con.execute('select sample_guid, tree from complete'):
        for sample_guid in sample_guids.split(','):
            max_size = 0
            if len(tree) > max_size:
                guid_tree_map[sample_guid].append(tree)
                max_size = len(tree)

make_guid_tree_map()

class captured_output:
    def __init__(self):
        self.prevfd = None
        self.prev = None

    def __enter__(self):
        F = tempfile.NamedTemporaryFile()
        self.prevfd = os.dup(sys.stdout.fileno())
        os.dup2(F.fileno(), sys.stdout.fileno())
        self.prev = sys.stdout
        sys.stdout = os.fdopen(self.prevfd, "w")
        return F

    def __exit__(self, exc_type, exc_value, traceback):
        os.dup2(self.prevfd, self.prev.fileno())
        sys.stdout = self.prev

def graph(guids, reference, quality, elephantwalkurl):
    with db_lock, con:
        all_neighbours = con.execute("select distance,neighbours_count from neighbours where samples = ? and reference = ? and quality = ? and elephantwalkurl = ? order by distance asc",
                                     (guids, reference, quality, elephantwalkurl)).fetchall()
    return [(x[0], x[1]) for x in all_neighbours]

#
# same as graph, different format
#
def graph2(guids, reference, quality, elephantwalkurl):
    with db_lock, con:
        all_neighbours = con.execute("select distance,neighbours_count from neighbours where samples = ? and reference = ? and quality = ? and elephantwalkurl = ? order by distance asc",
                                     (guids, reference, quality, elephantwalkurl)).fetchall()
    return ([x[0] for x in all_neighbours], [x[1] for x in all_neighbours])

def graph3(guids, reference, quality, elephantwalkurl, cutoff):
    running = True
    ns = []
    last = None
    last_count = 0
    distance = 0
    ps = []
    while running:
        last = len(ns)
        ns = neighbours(guids, reference, distance, quality, elephantwalkurl)
        ps.append([distance, len(ns)])
        distance = distance + 1
        if len(ns) == last:
            last_count = last_count + 1
        else:
            last_count = 0
        if last_count >= cutoff - 1:
            running = False
        print(last_count)
    return ([x[0] for x in ps], [x[1] for x in ps])


#
# check if neighbours in database. query elephantwalk if not
#
# just returns guids argument if it is a list (contains a ,)
#
def neighbours(guids, reference, distance, quality, elephantwalkurl):
    with db_lock, con:
        neighbours = con.execute("select * from neighbours where samples = ? and reference = ? and distance = ? and quality = ? and elephantwalkurl = ?",
                                 (guids, reference, int(distance), quality, elephantwalkurl)).fetchall()
        if neighbours:
            print("returning from db")
            return json.loads(neighbours[0][7])
        else:
            if "," in guids:
                print("sample is a list of guids: returning itself")
                return [[x.strip(), ''] for x in guids.split(",")]
            else:
                print("getting neighbours from elephantwalk")
                neighbour_guids = lib.get_neighbours(guids, reference, distance, quality, elephantwalkurl)
                uid = uuid.uuid4()
                n = con.execute("insert into neighbours values (?,?,?,?,?,?,?,?,?)",
                                (str(uid),guids,int(distance),reference,quality,elephantwalkurl,str(int(time.time())),json.dumps(neighbour_guids),len(neighbour_guids)))
                return neighbour_guids

def demon_interface():
    #
    # read tree file
    #
    def _get_tree(guid, reference, distance, quality):
        with db_lock, con:
            elem = con.execute('select * from queue where sample_guid = ? and reference = ? and distance = ? and quality = ?',
                               (guid,reference,distance,quality)).fetchall()
        if not elem:
            print("invariant failed: _get_tree")
            exit(1)
        tree = open("data/{0}/merged_fasta.treefile".format(elem[0][1])).read().strip()
        return tree

    #
    # get neighbours, make multifasta file and run iqtree
    #
    def go(guid, run_uuid, reference, distance, quality, elephantwalkurl, cores, iqtreepath):
        # get neighbours without distances
        neighbour_guids = [x[0] for x in neighbours(guid, reference, distance, quality, elephantwalkurl)]

        old_dir = os.getcwd()
        run_dir = "data/" + run_uuid

        print("makedirs")
        os.makedirs(run_dir, exist_ok=False)
        os.chdir(run_dir)

        data = { "guid": guid, "run_guid": run_uuid, "reference": reference,
                 "distance": distance, "quality": quality, "elephantwalkurl": elephantwalkurl,
                 "cores": cores, "iqtreepath": iqtreepath }

        with open("settings.json", "w") as f:
            f.write(json.dumps(data))

        if "," not in guid:
            neighbour_guids.append(guid)

        names = neighbour_guids

        lib.concat_fasta(neighbour_guids, names, reference, cfg['pattern'], "merged_fasta")

        metafile_tmp = tempfile.mktemp()
        lib.generate_openmpseq_metafile(neighbour_guids, names, reference,
                                        cfg['pattern'], metafile_tmp)
        openmpseq_out_dir = tempfile.mkdtemp()
        lib.run_openmpsequencer(cfg['openmpsequencer_bin_path'], metafile_tmp, openmpseq_out_dir)

        counts = lib.count_bases(pathlib.Path(openmpseq_out_dir) / "sequencer_count_bases.txt")
        print(counts)
        base_counts = [str(counts[base]) for base in ['A','C','G','T']]
        base_counts_str = ",".join(base_counts)
        print(base_counts_str)

        # clamp number of cores 1-20
        cores = sorted([1, len(neighbour_guids), 20])[1]

        while True:
            print("running iqtree")
            cmd_line = "{0} -s merged_fasta -st DNA -m GTR+I -blmin 0.00000001 -nt {1} -fconst {2}".format(
                iqtreepath, cores, base_counts_str)
            print(cmd_line)
            ret = os.system(cmd_line)
            if ret != 0:
                if cores > 1:
                    print("WARNING: iqtree failed to run with {0} cores. Trying with {1} cores".format(
                        cores, cores - 1))
                    cores = cores - 1
                else:
                    print("ERROR: iqtree failed with cores == 1")
                    break
            else:
                print("OK")
                break

        os.chdir(old_dir)
        return (ret, neighbour_guids)

    #
    # read queue table and run go() when there's an entry
    #
    while True:
        with db_lock, con:
            elem = con.execute('select * from queue where status = "queued" order by epoch_added desc limit 1').fetchall()

        if elem:
            elem = elem[0]
            print("starting {0}", elem)
            started = str(int(time.time()))
            with db_lock, con:
                con.execute('update queue set status = "RUNNING", epoch_start = ? where sample_guid = ? and reference = ? and distance = ? and quality = ?',
                            (started, elem[0], elem[4], elem[5], elem[6]))

            ret, neighbour_guids = go(elem[0], elem[1], elem[4], elem[5], elem[6], elem[3], cfg['iqtreecores'],
                                      "../../contrib/iqtree-1.6.5-Linux/bin/iqtree")

            ended = str(int(time.time()))
            if ret == 0:
                tree = _get_tree(elem[0], elem[4], elem[5], elem[6])
            else:
                tree = "(error):0;"
            with db_lock, con:
                con.execute('delete from queue where sample_guid = ? and reference = ? and distance = ? and quality = ?',
                            (elem[0], elem[4], elem[5], elem[6]))
                con.execute('insert into complete values (?,?,?,?,?,?,?,?,?,?,?)',
                            (elem[0], elem[1], elem[3], elem[4], elem[5], elem[6], elem[7], started, ended, json.dumps(neighbour_guids), tree))

            make_guid_tree_map()
            print("done with {0}", elem)

        if int(time.time()) % 100 == 0: print("daemon idle")
        time.sleep(5)

T = threading.Thread(target=demon_interface)
T.start()

app = Flask(__name__)
swagger = Swagger(app)

#
# return nth column from run table. add run to queue if it doesn't exist
#
def get_run_index(guid, n):
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    distance = request.args.get('distance')
    if not distance: distance = cfg['default_distance']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']

    with db_lock, con:
        queued = con.execute('select * from queue where sample_guid = ? and reference = ? and distance = ? and quality = ?',
                             (guid,reference,distance,quality)).fetchall()
        completed = con.execute('select * from complete where sample_guid = ? and reference = ? and distance = ? and quality = ?',
                                (guid,reference,distance,quality)).fetchall()

    if queued and completed:
        print("invariant failed: queued and completed")
        exit(1)

    if completed:
        return completed[0][n]
    elif queued:
        return "run is already queued\n"
    else:
        run_uuid = str(uuid.uuid4())
        with db_lock, con:
            con.execute('insert into queue values (?,?,?,?,?,?,?,?,?)',
                        (guid, run_uuid, "queued", cfg['elephantwalkurl'], reference, distance, quality, str(int(time.time())), ''))
        return "run added to queue\n"


@app.route('/')
def root_page():
    return redirect('/status')

#
# flask routes
#
@app.route('/trees_with_sample/<sample_guid>')
def trees_with_sample(sample_guid):
    xs = guid_tree_map[sample_guid]
    print(xs)
    return json.dumps([lib.rescale_newick(lib.relabel_newick(tree)) for tree in xs])

@app.route('/status')
def status():
    """Endpoint returning running, queued and completed trees
    ---
    responses:
      200:
        description: array of arrays containing sample guid, reference, distance, quality, start time, end time
    """
    with db_lock, con:
        running = con.execute('select sample_guid, reference, distance, quality from queue where status = "RUNNING"').fetchall()
        queued = con.execute('select sample_guid, reference, distance, quality from queue where status <> "RUNNING"').fetchall()
        completed_ = con.execute('select sample_guid, reference, distance, quality, epoch_start, epoch_end from complete order by epoch_end desc').fetchall()
    completed = []
    daemon_alive = T.is_alive()
    for run in completed_:
        completed.append(list(run))
    for run in completed:
        run.append(lib.hms_timediff(run[5], run[4]))
    return render_template('status.template', running=running, queued=queued, completed=completed, daemon_alive=daemon_alive)

# just guids
@app.route('/neighbours/<guid>')
def get_neighbours(guid):
    """Endpoint returning neighbour guids for guid
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description: array of guids
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    distance = request.args.get('distance')
    if not distance: distance = cfg['default_distance']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    return json.dumps([x[0] for x in neighbours(guid, reference, distance, quality, cfg['elephantwalkurl'])])

# guids + distances
@app.route('/neighbours2/<guid>')
def get_neighbours2(guid):
    """Endpoint returning neighbour guids and distances for guid
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description: array of [guid, distance]
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    distance = request.args.get('distance')
    if not distance: distance = cfg['default_distance']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    return json.dumps(neighbours(guid, reference, distance, quality, cfg['elephantwalkurl']))

@app.route('/tree/<guid>')
def get_tree(guid):
    """Endpoint returning rescaled newick tree string for guid
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description: rescaled newick tree string
    """
    return lib.relabel_newick(lib.rescale_newick(get_run_index(guid, 10)))

@app.route('/trees/<guid>')
def get_trees(guid):
    """Endpoint returning array of [sample_guid,reference,distance,quality] for completed trees of guid
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
    """
    with con, db_lock:
        ret = con.execute('select sample_guid,reference,distance,quality from complete where sample_guid = ?', (guid,)).fetchall()
    return json.dumps(ret)

@app.route('/ndgraph/<guid>')
def get_graph(guid):
    """Endpoint returning rescaled newick tree string for guid
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description: rescaled newick tree string
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    return json.dumps(graph(guid, reference, quality, cfg['elephantwalkurl']))

@app.route('/ndgraph2/<guid>')
def get_graph2(guid):
    """Endpoint returning graph (variant 2)
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description:
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    return json.dumps(graph2(guid, reference, quality, cfg['elephantwalkurl']))

@app.route('/ndgraph3/<guid>')
def get_graph3(guid):
    """Endpoint returning graph (variant 3)
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description:
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    cutoff = request.args.get('cutoff')
    if not cutoff: cutoff = 4
    if cutoff and int(cutoff) > 10:
        cutoff = 10
    return json.dumps(graph3(guid, reference, quality, cfg['elephantwalkurl'], int(cutoff)))

@app.route('/ndgraph.svg/<guid>')
def get_graph_svg(guid):
    """Endpoint returning matplotlib svg graph
    ---
    parameters:
      - name: guid
        in: path
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description:
    """
    reference = request.args.get('reference')
    if not reference: reference = cfg['default_reference']
    quality = request.args.get('quality')
    if not quality: quality = cfg['default_quality']
    cutoff = request.args.get('cutoff')
    if cutoff and int(cutoff) > 10:
        cutoff = 10
    if cutoff:
        (xs,ys) = graph3(guid, reference, quality, cfg['elephantwalkurl'], int(cutoff))
    else:
        (xs,ys) = graph2(guid, reference, quality, cfg['elephantwalkurl'])
    if len(ys) == 0:
        slopes = []
    else:
        slopes = [0]
    print(xs)
    for n in range(len(xs)):
        if n == 0: continue
        slopes.append((ys[n] - ys[n-1])/(xs[n]-xs[n-1]))
    fig = Figure(figsize=(12,7), dpi=100)
    fig.suptitle("Sample: {0}, reference: {1}, quality: {2}, ew: {3}".format(guid,reference,quality, cfg['elephantwalkurl']))
    ax = fig.add_subplot(111)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.plot(xs, ys, 'gx-', linewidth=1)
    ax.plot(xs, slopes, 'r-', linewidth=1)
    ax.set_xlabel("Distance")
    ax.set_ylabel("Neighbours")
    canvas = FigureCanvas(fig)
    svg_output = StringIO()
    canvas.print_svg(svg_output)
    response = make_response(svg_output.getvalue())
    response.headers['Content-Type'] = 'image/svg+xml'
    return response

@app.route('/new_run')
def new_run():
    """Endpoint starting a new run (query arguments)
    ---
    parameters:
      - name: guid
        in: query
        required: true
        type: string
      - name: reference
        in: query
        type: string
        required: false
      - name: distance
        in: query
        type: string
        required: false
      - name: quality
        in: query
        type: string
        required: false
    responses:
      200:
        description:
    """
    guid = request.args.get('guid')
    return get_run_index(guid, 10)

@app.route('/queue')
def get_queue():
    """Endpoint returning tree queue
    ---
    responses:
      200:
        description:
    """
    with db_lock, con:
        ret = []
        queued = con.execute('select sample_guid, reference, distance, quality, status, epoch_added, epoch_start, "" from queue').fetchall()
        for row in queued:
            neighbours_count = con.execute('select neighbours_count from neighbours where samples = ? and reference = ? and distance = ? and quality = ?',
                                           (row[0], row[1], row[2], row[3])).fetchall()
            count = -1
            if len(neighbours_count) > 0:
                count = neighbours_count[0][0]
            ret.append(list(row) + [count])
    return json.dumps(ret)

@app.route('/complete')
def get_complete():
    """Endpoint returning completed trees
    ---
    responses:
      200:
        description:
    """
    with db_lock, con:
        ret = []
        completed = con.execute('select sample_guid, reference, distance, quality, "DONE", epoch_added, epoch_start, epoch_end from complete order by epoch_end desc').fetchall()
        for row in completed:
            # if the "sample name" contains a list of guids, just return the first one
            if ',' in row[0]:
                xs = row[0].split(',')
                first = xs[0]
                row = (first, *row[1:])
                count = len(xs)
            else:
                neighbours_count = con.execute('select neighbours_count from neighbours where samples = ? and reference = ? and distance = ? and quality = ?',
                                           (row[0], row[1], row[2], row[3])).fetchall()
                count = -1
                if len(neighbours_count) > 0:
                    count = neighbours_count[0][0]

            ret.append(list(row) + [count])
    return json.dumps(ret)

@functools.lru_cache(maxsize=None)
def do_lookup(name, is_guid):
    with db_lock, con:
        if is_guid:
            rows = con.execute('select guid,name from sample_lookup_table where guid = ?', (name,)).fetchall()
        else:
            rows = con.execute("select guid,name from sample_lookup_table where upper(name) like ?", (name+"%",)).fetchall()
    return rows

@app.route('/lookup/<names>')
def lookup(names):
    """Endpoint mapping names to guids
    ---
    parameters:
      - name: names
        in: path
        required: true
        type: string
    responses:
      200:
        description: given a name return guid, given a guid, return name
    """
    ret = []
    names = names.replace("[", "")
    names = names.replace("]", "")
    names = names.replace('"', "")
    names = names.replace(" ", "")
    names = [x.strip() for x in names.split(',')]
    for name in names:
        try:
            guid = uuid.UUID(name)
        except:
            guid = None

        rows = do_lookup(name, guid)
        print(rows)
        if rows:
            ret.append(rows)
        else:
            ret.append([])
    return json.dumps([item for sublist in ret for item in sublist])

@app.route('/sync_sample_lookup_table')
def sync_lookup_table():
    """Download names and guids from cassandra (blocking)
    ---
    responses:
      200:
        description:
    """
    from cassandra.cluster import Cluster
    from cassandra.auth import PlainTextAuthProvider

    cas_auth_provider = PlainTextAuthProvider(username=cfg['cassandra_username'], password=cfg['cassandra_password'])
    cas_cluster = Cluster(cfg['cassandra_ips'], auth_provider=cas_auth_provider)
    cas_session = cas_cluster.connect('nosql_schema')

    rows = cas_session.execute('select name,id from sample')
    with db_lock, con:
        con.execute('delete from sample_lookup_table')
        for row in rows:
            con.execute('insert into sample_lookup_table values (?, ?)', (str(row.id), row.name))

    do_lookup.cache_clear()

    cas_cluster.shutdown()
    return redirect('/')

app.run(host='127.0.0.1', port=5008)
