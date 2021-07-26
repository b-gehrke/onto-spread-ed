# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License")
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# [START gae_python37_app]
import os
import io
import functools
import openpyxl
from openpyxl.styles import Font
from openpyxl.styles import PatternFill
import pandas as pd
import csv
import base64
import json
import traceback
import daff
import pyhornedowl
import networkx
import re

from flask import Flask, request, g, session, redirect, url_for, render_template
from flask import render_template_string, jsonify, Response
from flask_github import GitHub

from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from datetime import date

from urllib.request import urlopen

import threading

import whoosh
from whoosh.qparser import MultifieldParser,QueryParser

from datetime import datetime

# setup sqlalchemy

from config import *

engine = create_engine(DATABASE_URI)
db_session = scoped_session(sessionmaker(autocommit=False,
                                         autoflush=False,
                                         bind=engine))
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    github_access_token = Column(String(255))
    github_id = Column(Integer)
    github_login = Column(String(255))

    def __init__(self, github_access_token):
        self.github_access_token = github_access_token

class NextId(Base):
    __tablename__ = 'nextids'
    id = Column(Integer,primary_key=True)
    repo_name = Column(String(50))
    next_id = Column(Integer)

def init_db():

    Base.query = db_session.query_property()
    Base.metadata.create_all(bind=engine)


# Create an app instance
class FlaskApp(Flask):
    def __init__(self, *args, **kwargs):
        super(FlaskApp, self).__init__(*args, **kwargs)
        self._activate_background_job()

    def _activate_background_job(self):
        init_db()

# If `entrypoint` is not defined in app.yaml, App Engine will look for an app
# called `app` in `main.py`.
app = FlaskApp(__name__)

app.config.from_object('config')

github = GitHub(app)


# Implementation of Google Cloud Storage for index
class BucketStorage(whoosh.filedb.filestore.RamStorage):
    def __init__(self, bucket):
        super().__init__()
        self.bucket = bucket
        self.filenameslist = []

    def save_to_bucket(self):
        for name in self.files.keys():
            with self.open_file(name) as source:
                print("Saving file",name)
                blob = self.bucket.blob(name)
                blob.upload_from_file(source)
        for name in self.filenameslist:
            if name not in self.files.keys():
                blob = self.bucket.blob(name)
                print("Deleting old file",name)
                self.bucket.delete_blob(blob.name)
                self.filenameslist.remove(name)

    def open_from_bucket(self):
        self.filenameslist = []
        for blob in bucket.list_blobs():
            print("Opening blob",blob.name)
            self.filenameslist.append(blob.name)
            f = self.create_file(blob.name)
            blob.download_to_file(f)
            f.close()


class SpreadsheetSearcher:
    # bucket is defined in config.py
    def __init__(self):
        self.storage = BucketStorage(bucket)
        self.threadLock = threading.Lock()

    def searchFor(self, repo_name, search_string="", assigned_user=""):
        self.storage.open_from_bucket()
        ix = self.storage.open_index()

        mparser = MultifieldParser(["class_id","label","definition","parent","tobereviewedby"],
                                schema=ix.schema)

        query = mparser.parse("repo:"+repo_name+
                              (" AND ("+search_string+")" if search_string  else "")+
                              (" AND tobereviewedby:"+assigned_user if assigned_user else "") )

        with ix.searcher() as searcher:
            results = searcher.search(query, limit=100)
            resultslist = []
            for hit in results:
                allfields = {}
                for field in hit:
                    allfields[field]=hit[field]
                resultslist.append(allfields)

        ix.close()
        return (resultslist)

    def updateIndex(self, repo_name, folder, sheet_name, header, sheet_data):
        self.threadLock.acquire()
        print("Updating index...")
        self.storage.open_from_bucket()
        ix = self.storage.open_index()
        writer = ix.writer()
        mparser = MultifieldParser(["repo", "spreadsheet"],
                                   schema=ix.schema)
        print("About to delete for query string: ","repo:" + repo_name + " AND spreadsheet:'" + folder+"/"+sheet_name+"'")
        writer.delete_by_query(
            mparser.parse("repo:" + repo_name + " AND spreadsheet:\"" + folder+"/"+sheet_name+"\""))
        writer.commit()

        writer = ix.writer()

        for r in range(len(sheet_data)):
            row = [v for v in sheet_data[r].values()]
            del row[0] # Tabulator-added ID column

            if "ID" in header:
                class_id = row[header.index("ID")]
            else:
                class_id = None
            if "Label" in header:
                label = row[header.index("Label")]
            else:
                label = None
            if "Definition" in header:
                definition = row[header.index("Definition")]
            else:
                definition = None
            if "Parent" in header:
                parent = row[header.index("Parent")]
            else:
                parent = None
            if "To be reviewed by" in header:
                tobereviewedby = row[header.index("To be reviewed by")]
            else:
                tobereviewedby = None

            if class_id or label or definition or parent:
                writer.add_document(repo=repo_name,
                                    spreadsheet=folder+'/'+sheet_name,
                                    class_id=(class_id if class_id else None),
                                    label=(label if label else None),
                                    definition=(definition if definition else None),
                                    parent=(parent if parent else None),
                                    tobereviewedby=(tobereviewedby if tobereviewedby else None))
        writer.commit(optimize=True)
        self.storage.save_to_bucket()
        ix.close()
        self.threadLock.release()
        print("Update of index completed.")

    def getNextId(self,repo_name):
        self.threadLock.acquire()
        self.storage.open_from_bucket()
        ix = self.storage.open_index()

        nextId = 0

        mparser = QueryParser("class_id",
                              schema=ix.schema)

        query = mparser.parse(repo_name.upper()+"*")

        with ix.searcher() as searcher:
            results = searcher.search(query, sortedby="class_id",reverse=True)
            tophit = results[0]
            nextId = int(tophit['class_id'].split(":")[1] )+1

        ix.close()

        self.threadLock.release()
        return (nextId)


searcher = SpreadsheetSearcher()

class OntologyDataStore:
    node_props = {"shape":"box","style":"rounded", "font": "helvetica"}
    rel_cols = {"has part":"blue","part of":"blue","contains":"green",
                "has role":"darkgreen","is about":"darkgrey",
                "has participant":"darkblue"}

    def __init__(self):
        self.releases = {}
        self.releasedates = {}
        self.label_to_id = {}
        self.graphs = {}

    def parseRelease(self,repo):
        # Keep track of when you parsed this release
        self.graphs[repo] = networkx.MultiDiGraph()
        self.releasedates[repo] = date.today()
        #print("Release date ",self.releasedates[repo])

        # Get the ontology from the repository
        ontofilename = app.config['RELEASE_FILES'][repo]
        repositories = app.config['REPOSITORIES']
        repo_detail = repositories[repo]
        location = f"https://raw.githubusercontent.com/{repo_detail}/master/{ontofilename}"
        print("Fetching release file from", location)
        data = urlopen(location).read()  # bytes
        ontofile = data.decode('utf-8')

        # Parse it
        if ontofile:
            self.releases[repo] = pyhornedowl.open_ontology(ontofile)
            prefixes = app.config['PREFIXES']
            for prefix in prefixes:
                self.releases[repo].add_prefix_mapping(prefix[0],prefix[1])
            for classIri in self.releases[repo].get_classes():
                classId = self.releases[repo].get_id_for_iri(classIri)
                if classId:
                    classId = classId.replace(":","_")
                    # is it already in the graph?
                    if classId not in self.graphs[repo].nodes:
                        label = self.releases[repo].get_annotation(classIri, app.config['RDFSLABEL'])
                        if label:
                            self.label_to_id[label.strip()] = classId
                            self.graphs[repo].add_node(classId,
                                                       label=label.strip().replace(" ", "\n"),
                                                **OntologyDataStore.node_props)
                        else:
                            print("Could not determine label for IRI",classIri)
                else:
                    print("Could not determine ID for IRI",classIri)
            for classIri in self.releases[repo].get_classes():
                classId = self.releases[repo].get_id_for_iri(classIri)
                if classId:
                    parents = self.releases[repo].get_superclasses(classIri)
                    for p in parents:
                        plabel = self.releases[repo].get_annotation(p, app.config['RDFSLABEL'])
                        if plabel and plabel.strip() in self.label_to_id:
                            self.graphs[repo].add_edge(self.label_to_id[plabel.strip()],
                                                       classId.replace(":", "_"), dir="back")
                    axioms = self.releases[repo].get_axioms_for_iri(classIri) # other relationships
                    for a in axioms:
                        # Example: ['SubClassOf', 'http://purl.obolibrary.org/obo/CHEBI_27732', ['ObjectSomeValuesFrom', 'http://purl.obolibrary.org/obo/RO_0000087', 'http://purl.obolibrary.org/obo/CHEBI_60809']]
                        if len(a) == 3 and a[0]=='SubClassOf' \
                            and isinstance(a[2], list) and len(a[2])==3 \
                            and a[2][0]=='ObjectSomeValuesFrom':
                            relIri = a[2][1]
                            targetIri = a[2][2]
                            rel_name = self.releases[repo].get_annotation(relIri, app.config['RDFSLABEL'])
                            targetLabel = self.releases[repo].get_annotation(targetIri, app.config['RDFSLABEL'])
                            if targetLabel and targetLabel.strip() in self.label_to_id:
                                if rel_name in OntologyDataStore.rel_cols:
                                    rcolour = OntologyDataStore.rel_cols[rel_name]
                                else:
                                    rcolour = "orange"
                                self.graphs[repo].add_edge(classId.replace(":", "_"),
                                                           self.label_to_id[targetLabel.strip()],
                                                           color=rcolour,
                                                           label=rel_name)

    def getReleaseLabels(self, repo):
        all_labels = set()
        for classIri in self.releases[repo].get_classes():
            all_labels.add(self.releases[repo].get_annotation(classIri, app.config['RDFSLABEL']))
        return( all_labels )

    def parseSheetData(self, repo, data):
        for entry in data:
            if 'ID' in entry and \
                    'Label' in entry and \
                    'Definition' in entry and \
                    'Parent' in entry and \
                    len(entry['ID'])>0:
                entryId = entry['ID'].replace(":", "_")
                entryLabel = entry['Label'].strip()
                self.label_to_id[entryLabel] = entryId
                if entryId in self.graphs[repo].nodes:
                    self.graphs[repo].remove_node(entryId)
                    self.graphs[repo].add_node(entryId, label=entryLabel.replace(" ", "\n"), **OntologyDataStore.node_props)
        for entry in data:
            if 'ID' in entry and \
                    'Label' in entry and \
                    'Definition' in entry and \
                    'Parent' in entry and \
                    len(entry['ID'])>0:
                entryParent = re.sub("[\[].*?[\]]", "", entry['Parent']).strip()
                if entryParent in self.label_to_id:  # Subclass relations
                    # Subclass relations must be reversed for layout
                    self.graphs[repo].add_edge(self.label_to_id[entryParent],
                                               entry['ID'].replace(":", "_"), dir="back")
                for header in entry.keys():  # Other relations
                    if entry[header] and str(entry[header]).strip() and "REL" in header:
                        # Get the rel name
                        rel_names = re.findall(r"'([^']+)'", header)
                        if len(rel_names) > 0:
                            rel_name = rel_names[0]
                            if rel_name in OntologyDataStore.rel_cols:
                                rcolour = OntologyDataStore.rel_cols[rel_name]
                            else:
                                rcolour = "orange"

                            relValues = entry[header].split(";")
                            for relValue in relValues:
                                if relValue.strip() in self.label_to_id:
                                    self.graphs[repo].add_edge(entry['ID'].replace(":", "_"),
                                                               self.label_to_id[relValue.strip()],
                                                               color=rcolour,
                                                               label=rel_name)
 
 # todo: re-factoring the following:  
    
    # getIDsFromSheet - related ID's from whole sheet
    # getIDsFromSelection - related ID's from selection in sheet    
    # getRelatedIds - related ID's from list of ID's

    # getDotForSheetGraph - graph from whole sheet
    # getDotForSelection - graph from selection in sheet
    # getDotForIDs - graph from ID list
 
    
    def getIDsFromSheet(self, repo, data):
        # list of ids from sheet
        print("getIDsFromSheet here")
        ids = []
        for entry in data:
            if 'Curation status' in entry and str(entry['Curation status']) == "Obsolete": 
                print("Obsolete: ", entry)
            else:
                if 'ID' in entry and len(entry['ID'])>0:
                    ids.append(entry['ID'].replace(":","_"))

                if 'Parent' in entry:
                    entryParent = re.sub("[\[].*?[\]]", "", entry['Parent']).strip()
                    if entryParent in self.label_to_id:
                        ids.append(self.label_to_id[entryParent])
        return (ids)
    
    
    def getIDsFromSelection(self, repo, data, selectedIds):
        # Add all descendents of the selected IDs, the IDs and their parents.
        print(selectedIds)
        ids = [] 
        for id in selectedIds:
            print("looking at id: ", id)
            entry = data[id]
            # don't visualise rows which are set to "Obsolete":
            if 'Curation status' in entry and str(entry['Curation status']) == "Obsolete": 
                print("Obsolete: ", id)
            else:
                if str(entry['ID']) and str(entry['ID']).strip(): #check for none and blank ID's
                    if 'ID' in entry and len(entry['ID']) > 0:
                        ids.append(entry['ID'].replace(":", "_"))
                    if 'Parent' in entry:
                        entryParent = re.sub("[\[].*?[\]]", "", entry['Parent']).strip()
                        if entryParent in self.label_to_id:
                            ids.append(self.label_to_id[entryParent])
                    entryIri = self.releases[repo].get_iri_for_id(entry['ID'])
                    if entryIri:
                        descs = pyhornedowl.get_descendants(self.releases[repo], entryIri)
                        for d in descs:
                            ids.append(self.releases[repo].get_id_for_iri(d).replace(":", "_"))
                    if self.graphs[repo]:
                        graph_descs = None
                        try:
                            graph_descs = networkx.algorithms.dag.descendants(self.graphs[repo],entry['ID'].replace(":", "_"))
                        except networkx.exception.NetworkXError:
                            print("networkx exception error in getIDsFromSelection", id)
                        
                        if graph_descs is not None:
                            for g in graph_descs:
                                if g not in ids:
                                    ids.append(g)                        
        return (ids)

    def getRelatedIDs(self, repo, selectedIds):
        # Add all descendents of the selected IDs, the IDs and their parents.
        ids = []
        for id in selectedIds:
            ids.append(id.replace(":","_"))
            entryIri = self.releases[repo].get_iri_for_id(id)
            print("Got IRI",entryIri,"for ID",id)
            #todo: get label, definitions, synonyms here?

            if entryIri:
                descs = pyhornedowl.get_descendants(self.releases[repo],entryIri)
                for d in descs:
                    ids.append(self.releases[repo].get_id_for_iri(d).replace(":","_"))
                    #todo: get label, definitions, synonyms here?
                    
                superclasses = self.releases[repo].get_superclasses(entryIri)
                # superclasses = pyhornedowl.get_superclasses(self.releases[repo], entryIri) 
                for s in superclasses:
                    ids.append(self.releases[repo].get_id_for_iri(s).replace(":", "_"))
            if self.graphs[repo]:
                graph_descs = None
                try:
                    print("repo is: ", repo, " id: ", id)
                    graph_descs = networkx.algorithms.dag.descendants(self.graphs[repo], id.replace(":", "_"))
                    print("Got descs from graph",graph_descs)
                    print(type(graph_descs))
                except networkx.exception.NetworkXError:
                    print("got networkx exception in getRelatedIDs ", id)

                if graph_descs is not None:
                    for g in graph_descs:
                        if g not in ids:
                            ids.append(g)
        return (ids)

    def getDotForSheetGraph(self, repo, data):
        # Get a list of IDs from the sheet graph
        ids = OntologyDataStore.getIDsFromSheet(self, repo, data)
        subgraph = self.graphs[repo].subgraph(ids)
        P = networkx.nx_pydot.to_pydot(subgraph)
        return (P)

    def getDotForSelection(self, repo, data, selectedIds):
        # Add all descendents of the selected IDs, the IDs and their parents.
        ids = OntologyDataStore.getIDsFromSelection(self, repo, data, selectedIds)
        # Then get the subgraph as usual
        subgraph = self.graphs[repo].subgraph(ids)
        P = networkx.nx_pydot.to_pydot(subgraph)
        return (P)

    def getDotForIDs(self, repo, selectedIds):
        # Add all descendents of the selected IDs, the IDs and their parents.
        ids = OntologyDataStore.getRelatedIDs(self, repo, selectedIds)
        # Then get the subgraph as usual
        subgraph = self.graphs[repo].subgraph(ids)
        P = networkx.nx_pydot.to_pydot(subgraph)
        return (P)   

    #todo: get labels, definitions, synonyms to go with ID's here:
    #need to create a dictionary and add all info to it, in the relevant place
    def getMetaData(self, repo, allIDS):                    
        DEFN = "http://purl.obolibrary.org/obo/IAO_0000115"
        SYN = "http://purl.obolibrary.org/obo/IAO_0000118"

        label = "" 
        definition = ""
        synonyms = ""
        entries = []

        
        all_labels = set()
        for classIri in self.releases[repo].get_classes():
            classId = self.releases[repo].get_id_for_iri(classIri).replace(":", "_")
            #todo: check for null ID's!
            for id in allIDS:   
                if id is not None:         
                    if classId == id:
                        # print("GOT A MATCH: ", classId)
                        label = self.releases[repo].get_annotation(classIri, app.config['RDFSLABEL']) #yes
                        # print("label for this MATCH is: ", label)
                        iri = self.releases[repo].get_iri_for_label(label)
                        #todo: need to get definition and synonyms still below:
                        if self.releases[repo].get_annotation(classIri, DEFN) is not None:             
                            definition = self.releases[repo].get_annotation(classIri, DEFN).replace(",", "").replace("'", "").replace("\"", "") #.replace("&", "and").replace(":", " ").replace("/", " ").replace(".", " ").replace("-", " ").replace("(", " ").replace(")", " ")    
                            # definition = self.releases[repo].get_annotation(classIri, app.config['DEFN']) 
                            # print("definition for this MATCH is: ", definition)
                        else:
                            definition = ""
                        if self.releases[repo].get_annotation(classIri, SYN) is not None:
                            synonyms = self.releases[repo].get_annotation(classIri, SYN).replace(",", "").replace("'", "").replace("\"", "") #.replace("&", "and") #.replace(":", " ").replace("/", " ").replace(".", " ").replace("-", " ").replace("(", " ").replace(")", " ")
                            # print("synonym for this MATCH is: ", synonyms)
                        else:
                            synonyms = ""
                        entries.append({
                            "id": id,
                            "label": label, 
                            "synonyms": synonyms,
                            "definition": definition,                      
                        })
        return (entries)



ontodb = OntologyDataStore()

def verify_logged_in(fn):
    """
    Decorator used to make sure that the user is logged in
    """
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        # If the user is not logged in, then redirect him to the "logged out" page:
        if not g.user:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapped

# Pages:


@app.before_request
def before_request():
    g.user = None
    if 'user_id' in session:
        #print("user-id in session: ",session['user_id'])
        g.user = User.query.get(session['user_id'])


@app.after_request
def after_request(response):
    db_session.remove()
    return response


@github.access_token_getter
def token_getter():
    user = g.user
    if user is not None:
        return user.github_access_token


@app.route('/github-callback')
@github.authorized_handler
def authorized(access_token):
    next_url = request.args.get('next') or url_for('home')
    if access_token is None:
        print("Authorization failed.")
        return redirect(url_for('logout'))

    user = User.query.filter_by(github_access_token=access_token).first()
    if user is None:
        user = User(access_token)
    # Not necessary to get these details here
    # but it helps humans to identify users easily.
    g.user = user
    github_user = github.get('/user')
    user.github_id = github_user['id']
    user.github_login = github_user['login']
    user.github_access_token = access_token
    db_session.add(user)
    db_session.commit()

    session['user_id'] = user.id
    return redirect(next_url)


@app.route('/login')
def login():
    if session.get('user_id', None) is not None:
        session.pop('user_id',None) # Could be stale
    return github.authorize(scope="user,repo")

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('loggedout'))

@app.route("/loggedout")
def loggedout():
    """
    Displays the page to be shown to logged out users.
    """
    return render_template("loggedout.html")


@app.route('/user')
@verify_logged_in
def user():
    return jsonify(github.get('/user'))



# Pages for the app


@app.route('/search', methods=['POST'])
@verify_logged_in
def search():
    searchTerm = request.form.get("inputText")
    repoName = request.form.get("repoName")
    searchResults = searchAcrossSheets(repoName, searchTerm)    
    searchResultsTable = json.dumps(searchResults)
    return ( json.dumps({"message":"Success",
                             "searchResults": searchResultsTable}), 200 )


@app.route('/searchAssignedToMe', methods=['POST'])
@verify_logged_in
def searchAssignedToMe():
    #print("searching for initials")
    initials = request.form.get("initials")
    print("Searching for initials: " + initials)
    repoName = request.form.get("repoName")
    #below is searching in "Label" column? 
    searchResults = searchAssignedTo(repoName, initials)
    searchResultsTable = json.dumps(searchResults)
    return ( json.dumps({"message":"Success",
                             "searchResults": searchResultsTable}), 200 )
                      

@app.route('/')
@app.route('/home')
@verify_logged_in
def home():
    repositories = app.config['REPOSITORIES']
    user_repos = repositories.keys()
    # Filter just the repositories that the user can see
    if g.user.github_login in USERS_METADATA:
        user_repos = USERS_METADATA[g.user.github_login]["repositories"]

    repositories = {k:v for k,v in repositories.items() if k in user_repos}

    return render_template('index.html',
                           login=g.user.github_login,
                           repos=repositories)


@app.route('/repo/<repo_key>')
@app.route('/repo/<repo_key>/<path:folder_path>')
@verify_logged_in
def repo(repo_key, folder_path=""):
    repositories = app.config['REPOSITORIES']
    repo_detail = repositories[repo_key]
    directories = github.get(
        f'repos/{repo_detail}/contents/{folder_path}'
    )
    dirs = []
    spreadsheets = []
    #go to edit_external: 
    if folder_path == 'imports': 
        return redirect(url_for('edit_external', repo_key=repo_key, folder_path=folder_path))
    for directory in directories:
        if directory['type']=='dir':
            dirs.append(directory['name'])
        elif directory['type']=='file' and '.xlsx' in directory['name']:
            spreadsheets.append(directory['name'])
    if g.user.github_login in USERS_METADATA:
        user_initials = USERS_METADATA[g.user.github_login]["initials"]
    else:
        print(f"The user {g.user.github_login} has no known metadata")
        user_initials = g.user.github_login[0:2]

    return render_template('repo.html',
                            login=g.user.github_login,
                            user_initials=user_initials,
                            repo_name = repo_key,
                            folder_path = folder_path,
                            directories = dirs,
                            spreadsheets = spreadsheets,
                            )

@app.route("/direct", methods=["POST"])
@verify_logged_in
def direct():
    if request.method == "POST":
        type = json.loads(request.form.get("type"))
        repo = json.loads(request.form.get("repo"))
        sheet = json.loads(request.form.get("sheet"))
        go_to_row = json.loads(request.form.get("go_to_row"))
    repoStr = repo['repo']
    sheetStr = sheet['sheet']
    url = '/edit' + '/' + repoStr + '/' + sheetStr 
    session['type'] = type['type']
    session['label'] = go_to_row['go_to_row']
    session['url'] = url
    return('success')

@app.route("/validate", methods=["POST"]) 
@verify_logged_in
def verify():
    if request.method == "POST":
        cell = json.loads(request.form.get("cell"))
        column = json.loads(request.form.get("column"))
        rowData = json.loads(request.form.get("rowData"))
        headers = json.loads(request.form.get("headers")) 
        table = json.loads(request.form.get("table")) 
    # check for blank cells under conditions first:
    blank = {}
    unique = {}
    returnData, uniqueData = checkBlankMulti(1, blank, unique, cell, column, headers, rowData, table)
    if len(returnData) > 0 or len(uniqueData) > 0:
        return (json.dumps({"message":"fail","values":returnData, "unique":uniqueData}))
    return ('success') #todo: do we need message:success, 200 here? 
    

@app.route("/generate", methods=["POST"])
@verify_logged_in
def generate():
    if request.method == "POST":
        repo_key = request.form.get("repo_key")
        rowData = json.loads(request.form.get("rowData"))
        #print("generate data sent")
        #print("Got ", len(rowData), "rows:", rowData)
        values = {}
        ids = {}
        for row in rowData:
            nextIdStr = str(searcher.getNextId(repo_key))
            id = repo_key.upper()+":"+nextIdStr.zfill(app.config['DIGIT_COUNT'])
            #print("Row ID is ",row['id'])
            ids["ID"+str(row['id'])] = str(row['id'])
            values["ID"+str(row['id'])] = id
        #print("Got values: ",values)
        return (json.dumps({"message": "idlist", "IDs": ids, "values": values})) #need to return an array 
    return ('success')  

# validation checks here: 

# recursive check each cell in rowData:
def checkBlankMulti(current, blank, unique, cell, column, headers, rowData, table):
    for index, (key, value) in enumerate(rowData.items()): # todo: really, we need to loop here, surely there is a faster way?
        if index == current:
            if key == "Label" or key == "Definition" or key == "Parent" or key == "Sub-ontology" or key == "Curation status" :
                if key == "Definition" or key == "Parent":
                    status = rowData.get("Curation status") #check for "Curation status"
                    if(status):
                        if rowData["Curation status"] == "Proposed" or rowData["Curation status"] == "External":
                            pass
                        else:
                            if value.strip() == "":
                                blank.update({key:value})
                    else:
                        pass #no "Curation status" column
                else:       
                    if value.strip()=="":
                        blank.update({key:value})
                    else:
                        pass
            if key == "Label" or key == "ID" or key == "Definition":
                if checkNotUnique(value, key, headers, table):
                    unique.update({key:value})
    # go again:
    current = current + 1
    if current >= len(rowData):   
        return (blank, unique)
    return checkBlankMulti(current, blank, unique, cell, column, headers, rowData, table)

def checkNotUnique(cell, column, headers, table):
    counter = 0
    cellStr = cell.strip()
    if cellStr == "":
        return False
    # if Label, ID or Definition column, check cell against all other cells in the same column and return true if same
    for r in range(len(table)): 
        row = [v for v in table[r].values()]
        del row[0] # remove extra numbered "id" column
        for c in range(len(headers)):
            if headers[c] == "ID" and column == "ID":
                if row[c].strip()==cellStr:
                    counter += 1 
                    if counter > 1: #more than one of the same
                        return True
            if headers[c] == "Label" and column == "Label":
                if row[c].strip()==cellStr:
                    counter += 1 
                    if counter > 1: 
                        return True
            if headers[c] == "Definition" and column == "Definition":
                if row[c].strip()==cellStr:
                    counter += 1 
                    if counter > 1: 
                        return True
    return False

@app.route('/edit/<repo_key>/<path:folder>/<spreadsheet>')
@verify_logged_in
def edit(repo_key, folder, spreadsheet):
    if session.get('label') == None:
        go_to_row = ""
    else:
        go_to_row = session.get('label')
        session.pop('label', None)

    if session.get('type') == None:
        type = ""
    else:
        type = session.get('type')
        session.pop('type', None)
    #print("type is: ", type)
    #test values for type: 
    # type = "initials"
    # go_to_row = "RW"
    repositories = app.config['REPOSITORIES']
    repo_detail = repositories[repo_key]
    (file_sha,rows,header) = get_spreadsheet(repo_detail,folder,spreadsheet)
    if g.user.github_login in USERS_METADATA:
        user_initials = USERS_METADATA[g.user.github_login]["initials"]
    else:
        print(f"The user {g.user.github_login} has no known metadata")
        user_initials = g.user.github_login[0:2]
    #Build suggestions data:
    if repo_key not in ontodb.releases or date.today() > ontodb.releasedates[repo_key]:
        ontodb.parseRelease(repo_key)
    suggestions = ontodb.getReleaseLabels(repo_key)
    suggestions = list(dict.fromkeys(suggestions))

    return render_template('edit.html',
                            login=g.user.github_login,
                            user_initials=user_initials,
                            all_initials=ALL_USERS_INITIALS,
                            repo_name = repo_key,
                            folder = folder,
                            spreadsheet_name=spreadsheet,
                            header=json.dumps(header),
                            rows=json.dumps(rows),
                            file_sha = file_sha,
                            go_to_row = go_to_row,
                            type = type, 
                            suggestions = json.dumps(suggestions)
                            )


@app.route('/save', methods=['POST'])
@verify_logged_in
def save():
    repo_key = request.form.get("repo_key")
    folder = request.form.get("folder")
    spreadsheet = request.form.get("spreadsheet")
    row_data = request.form.get("rowData")
    initial_data = request.form.get("initialData")
    file_sha = request.form.get("file_sha").strip()
    commit_msg = request.form.get("commit_msg")
    commit_msg_extra = request.form.get("commit_msg_extra")
    overwrite = False
    overwriteVal = request.form.get("overwrite") 
    #print(f'overwriteVal is: ' + str(overwriteVal))
    if overwriteVal == "true":
        overwrite = True
        #print(f'overwrite True here')

    repositories = app.config['REPOSITORIES']
    repo_detail = repositories[repo_key]
    restart = False # for refreshing the sheet (new ID's)
    try:
        initial_data_parsed = json.loads(initial_data)
        row_data_parsed = json.loads(row_data)
        # Get the data, skip the first 'id' column
        initial_first_row = initial_data_parsed[0]
        initial_header = [k for k in initial_first_row.keys()]
        del initial_header[0]
        # Sort based on label
        # What if 'Label' column not present?
        if 'Label' in initial_first_row:
            initial_data_parsed = sorted(initial_data_parsed, key=lambda k: k['Label'] if k['Label'] else "")
        else:
            print("No Label column present, so not sorting this.") #do we need to sort - yes, for diff!

        first_row = row_data_parsed[0]
        header = [k for k in first_row.keys()]
        del header[0]
        # Sort based on label
        # What if 'Label' column not present?
        if 'Label' in first_row:
            row_data_parsed = sorted(row_data_parsed, key=lambda k: k['Label'] if k['Label'] else "")
        else:
            print("No Label column present, so not sorting this.") #do we need to sort - yes, for diff! 

        
        print("Got file_sha",file_sha)

        wb = openpyxl.Workbook()
        sheet = wb.active

        for c in range(len(header)):
            sheet.cell(row=1, column=c+1).value=header[c]
            sheet.cell(row=1, column=c+1).font = Font(size=12,bold=True)
        for r in range(len(row_data_parsed)):
            row = [v for v in row_data_parsed[r].values()]
            del row[0] # Tabulator-added ID column
            for c in range(len(header)):
                sheet.cell(row=r+2, column=c+1).value=row[c]
                # Set row background colours according to 'Curation status'
                # These should be kept in sync with those used in edit screen
                # TODO add to config
                # What if "Curation status" not present?
                if 'Curation status' in first_row:
                    if row[header.index("Curation status")]=="Discussed":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="ffe4b5", fill_type = "solid")
                    elif row[header.index("Curation status")]=="Ready": #this is depreciated
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="98fb98", fill_type = "solid")
                    elif row[header.index("Curation status")]=="Proposed":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="ffffff", fill_type = "solid")
                    elif row[header.index("Curation status")]=="To Be Discussed":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="eee8aa", fill_type = "solid")
                    elif row[header.index("Curation status")]=="In Discussion":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="fffacd", fill_type = "solid")                                
                    elif row[header.index("Curation status")]=="Published":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="7fffd4", fill_type = "solid")
                    elif row[header.index("Curation status")]=="Obsolete":
                        sheet.cell(row=r+2, column=c+1).fill = PatternFill(fgColor="2f4f4f", fill_type = "solid")

            # Generate identifiers:
            if 'ID' in first_row:             
                if not row[header.index("ID")]: #blank
                    if 'Label' and 'Parent' and 'Definition' in first_row: #make sure we have the right sheet
                        if row[header.index("Label")] and row[header.index("Parent")] and row[header.index("Definition")]: #not blank
                            #generate ID here: 
                            nextIdStr = str(searcher.getNextId(repo_key))
                            id = repo_key.upper()+":"+nextIdStr.zfill(app.config['DIGIT_COUNT'])
                            new_id = id
                            for c in range(len(header)):
                                if c==0:
                                    restart = True
                                    sheet.cell(row=r+2, column=c+1).value=new_id

        # Create version for saving
        spreadsheet_stream = io.BytesIO()
        wb.save(spreadsheet_stream)

        #base64_bytes = base64.b64encode(sample_string_bytes)
        base64_bytes = base64.b64encode(spreadsheet_stream.getvalue())
        base64_string = base64_bytes.decode("ascii")

        # Create a new branch to commit the change to (in case of simultaneous updates)
        response = github.get(f"repos/{repo_detail}/git/ref/heads/master")
        if not response or "object" not in response or "sha" not in response["object"]:
            raise Exception(f"Unable to get SHA for HEAD of master in {repo_detail}")
        sha = response["object"]["sha"]
        branch = f"{g.user.github_login}_{datetime.utcnow().strftime('%Y-%m-%d_%H%M%S')}"
        print("About to try to create branch in ",f"repos/{repo_detail}/git/refs")
        response = github.post(
            f"repos/{repo_detail}/git/refs", data={"ref": f"refs/heads/{branch}", "sha": sha},
            )
        if not response:
            raise Exception(f"Unable to create new branch {branch} in {repo_detail}")

        print("About to get latest version of the spreadsheet file",f"repos/{repo_detail}/contents/{folder}/{spreadsheet}")
        # Get the sha for the file
        (new_file_sha, new_rows, new_header) = get_spreadsheet(repo_detail,folder, spreadsheet)

        # Commit changes to branch (replace code with sheet)
        data = {
            "message": commit_msg,
            "content": base64_string,
            "branch": branch,
        }
        data["sha"] = new_file_sha
        print("About to commit file to branch",f"repos/{repo_detail}/contents/{folder}/{spreadsheet}")
        response = github.put(f"repos/{repo_detail}/contents/{folder}/{spreadsheet}", data=data)
        if not response:
            raise Exception(
                f"Unable to commit addition of {spreadsheet} to branch {branch} in {repo_detail}"
            )

        # Create a PR for the change
        print("About to create PR from branch",)
        response = github.post(
            f"repos/{repo_detail}/pulls",
            data={
                "title": commit_msg,
                "head": branch,
                "base": "master",
                "body": commit_msg_extra
            },
        )
        if not response:
            raise Exception(f"Unable to create PR for branch {branch} in {repo_detail}")
        pr_info = response['html_url']

        # Do not merge automatically if this file was stale as that will overwrite the other changes
        
        if new_file_sha != file_sha and not overwrite:
            print("PR created and must be merged manually as repo file had changed")

            # Get the changes between the new file and this one:
            merge_diff, merged_table = getDiff(row_data_parsed, new_rows, new_header, initial_data_parsed) # getDiff(saving version, latest server version, header for both)
            # update rows for comparison:
            (file_sha3,rows3,header3) = get_spreadsheet(repo_detail,folder,spreadsheet)
            #todo: delete transient branch here? Github delete code is a test for now. 
            # Delete the branch again
            print ("About to delete branch",f"repos/{repo_detail}/git/refs/heads/{branch}")
            response = github.delete(
                f"repos/{repo_detail}/git/refs/heads/{branch}")
            if not response:
                raise Exception(f"Unable to delete branch {branch} in {repo_detail}")
            return(
                json.dumps({'Error': 'Your change was submitted to the repository but could not be automatically merged due to a conflict. You can view the change <a href="'\
                    + pr_info + '" target = "_blank" >here </a>. ', "file_sha_1": file_sha, "file_sha_2": new_file_sha, "pr_branch":branch, "merge_diff":merge_diff, "merged_table":json.dumps(merged_table),\
                        "rows3": rows3, "header3": header3}), 300 #400 for missing REPO
                )
        else:
            # Merge the created PR
            print("About to merge created PR")
            response = github.post(
                f"repos/{repo_detail}/merges",
                data={
                    "head": branch,
                    "base": "master",
                    "commit_message": commit_msg
                },
            )
            if not response:
                raise Exception(f"Unable to merge PR from branch {branch} in {repo_detail}")

            # Delete the branch again
            print ("About to delete branch",f"repos/{repo_detail}/git/refs/heads/{branch}")
            response = github.delete(
                f"repos/{repo_detail}/git/refs/heads/{branch}")
            if not response:
                raise Exception(f"Unable to delete branch {branch} in {repo_detail}")

        print ("Save succeeded.")
        # Update the search index for this file ASYNCHRONOUSLY (don't wait)
        thread = threading.Thread(target=searcher.updateIndex,
                                  args=(repo_key, folder, spreadsheet, header, row_data_parsed))
        thread.daemon = True  # Daemonize thread
        thread.start()  # Start the execution

        # Get the sha AGAIN for the file
        response = github.get(f"repos/{repo_detail}/contents/{folder}/{spreadsheet}")
        if not response or "sha" not in response:
            raise Exception(
                f"Unable to get the newly updated SHA value for {spreadsheet} in {repo_detail}/{folder}"
                )
        new_file_sha = response['sha']
        if restart: #todo: does this need to be anywhere else also?
            return ( json.dumps({"message":"Success",
                                "file_sha": new_file_sha}), 360 )
        else:
            return ( json.dumps({"message":"Success",
                                "file_sha": new_file_sha}), 200 )

    except Exception as err:
        print(err)
        traceback.print_exc()
        return (
            json.dumps({"message": "Failed",
                        "Error":format(err)}),
            400,
        )



@app.route('/keepalive', methods=['POST'])
@verify_logged_in
def keep_alive():
    #print("Keep alive requested from edit screen")
    return ( json.dumps({"message":"Success"}), 200 )


#todo: use this function to compare initial spreadsheet to server version - check for updates?
@app.route("/checkForUpdates", methods=["POST"])
def checkForUpdates():
    if request.method == "POST":
        repo_key = request.form.get("repo_key") #todo: fix keyError!
        folder = request.form.get("folder")
        spreadsheet = request.form.get("spreadsheet")
        # initialData = request.form.get("initialData") 
        old_sha = request.form.get("file_sha")     
        #print(repo_key, folder, spreadsheet, old_sha)
        repositories = app.config['REPOSITORIES']
        repo_detail = repositories[repo_key]
        spreadsheet_file = github.get(
            f'repos/{repo_detail}/contents/{folder}/{spreadsheet}'
        )
        file_sha = spreadsheet_file['sha']
        #print("Check update - Got file_sha",file_sha)
        if old_sha == file_sha:
            return ( json.dumps({"message":"Success"}), 200 )
        else:
            return ( json.dumps({"message":"Fail"}), 200 )

@app.route('/openVisualiseAcrossSheets', methods=['POST'])
@verify_logged_in
def openVisualiseAcrossSheets():
    #build data we need for dotStr query (new one!)
    if request.method == "POST":
        idString = request.form.get("idList")
        print("idString is: ", idString)
        repo = request.form.get("repo") 
        print("repo is ", repo)
        idList = idString.split()
        print("idList is: ", idList)
        # for i in idList:
        #     print("i is: ", i)
        # indices = json.loads(request.form.get("indices"))
        # print("indices are: ", indices)
        ontodb.parseRelease(repo)
        #todo: do we need to support more than one repo at a time here?
        dotStr = ontodb.getDotForIDs(repo,idList).to_string()
        return render_template("visualise.html", sheet="selection", repo=repo, dotStr=dotStr)

    return ("Only POST allowed.")


# api: 
@app.route('/api/get-json')
# @verify_logged_in #how to check this?
def hello():
  return jsonify(hello='world') # Returns HTTP Response with {"hello": "world"}

@app.route('/api/openVisualiseAcrossSheets', methods=['GET'])
# @verify_logged_in #todo: how to do this?
def apiOpenVisualiseAcrossSheets():
    #build data we need for dotStr query (new one!)
    if request.method == "GET":
        idString = request.form.get("idList")
        print("idString is: ", idString)
        repo = request.form.get("repo")
        print("repo is ", repo)
        idList = idString.split()
        # for i in idList:
        #     print("i is: ", i)
        # indices = json.loads(request.form.get("indices"))
        # print("indices are: ", indices)
        ontodb.parseRelease(repo)
        dotStr = ontodb.getDotForIDs(repo,idList).to_string()
        #todo: need to generate dot graph here
        # return jsonify(dotStr=dotStr)
        return render_template("visualise.html", sheet="selection", repo=repo, dotStr=dotStr)



@app.route('/openVisualise', methods=['POST'])
@verify_logged_in
def openVisualise():
    if request.method == "POST":
        repo = request.form.get("repo")
        # print("repo is ", repo)
        sheet = request.form.get("sheet")
        # print("sheet is ", sheet)
        table = json.loads(request.form.get("table"))
        # print("table is: ", table)
        indices = json.loads(request.form.get("indices"))
        # print("indices are: ", indices)

        if repo not in ontodb.releases:
            ontodb.parseRelease(repo)
        if len(indices) > 0:
            ontodb.parseSheetData(repo,table)
            dotStr = ontodb.getDotForSelection(repo,table,indices).to_string()
            # print("first dotstr is: ", dotStr)
            #todo: this is a hack: works fine the second time? do it twice!
            ontodb.parseSheetData(repo,table)
            dotStr = ontodb.getDotForSelection(repo,table,indices).to_string()
        else:
            ontodb.parseSheetData(repo,table)
            dotStr = ontodb.getDotForSheetGraph(repo,table).to_string()
            # print("first dotstr is: ", dotStr)
            #todo: this is a hack: works fine the second time? do it twice!
            ontodb.parseSheetData(repo,table)
            dotStr = ontodb.getDotForSheetGraph(repo,table).to_string()

        # print("dotStr is: ", dotStr)
        return render_template("visualise.html", sheet=sheet, repo=repo, dotStr=dotStr)

    return ("Only POST allowed.")


@app.route('/visualise/<repo>/<sheet>')
@verify_logged_in
def visualise(repo, sheet):
    print("reached visualise")
    return render_template("visualise.html", sheet=sheet, repo=repo)

@app.route('/openPat', methods=['POST'])
@verify_logged_in
def openPat():
    if request.method == "POST":
        repo = request.form.get("repo")
        # print("repo is ", repo)
        sheet = request.form.get("sheet")
        # print("sheet is ", sheet)
        table = json.loads(request.form.get("table"))
        # print("table is: ", table)
        indices = json.loads(request.form.get("indices"))
        # print("indices are: ", indices)

        if repo not in ontodb.releases:
            ontodb.parseRelease(repo)
        if len(indices) > 0: #selection
            # ontodb.parseSheetData(repo,table)
            allIDS = ontodb.getIDsFromSelection(repo,table,indices)
            # print("got allIDS: ", allIDS)
        else: # whole sheet..
            ontodb.parseSheetData(repo,table)
            allIDS = ontodb.getIDsFromSheet(repo, table)
            #todo: do we need to do above twice? 
        
        # print("allIDS: ", allIDS)
        #remove duplicates from allIDS: 
        allIDS = list(dict.fromkeys(allIDS))
        
        allData = ontodb.getMetaData(repo, allIDS)  
        # print("allData: ", allData) 
        # print("dotStr is: ", dotStr)
        return render_template("pat.html", repo=repo, all_ids=allIDS, all_data=allData) #todo: PAT.html

    return ("Only POST allowed.")

@app.route('/openPatAcrossSheets', methods=['POST'])
@verify_logged_in
def openPatAcrossSheets():
    #build data we need for dotStr query (new one!)
    if request.method == "POST":
        idString = request.form.get("idList")
        # print("idString is: ", idString)
        repo = request.form.get("repo") 
        print("repo is ", repo)
        idList = idString.split()
        print("idList: ", idList)
        # for i in idList:
        #     print("i is: ", i)
        # indices = json.loads(request.form.get("indices"))
        # print("indices are: ", indices)
        ontodb.parseRelease(repo)
        #todo: do we need to support more than one repo at a time here?
        allIDS = ontodb.getRelatedIDs(repo,idList)
        # print("allIDS: ", allIDS)
        #remove duplicates from allIDS: 
        allIDS = list(dict.fromkeys(allIDS))

        #todo: all experimental from here: 
        allData = ontodb.getMetaData(repo, allIDS)  
        # print("TEST allData: ", allData)

        # dotStr = ontodb.getDotForIDs(repo,idList).to_string()
        return render_template("pat.html", repo=repo, all_ids=allIDS, all_data=allData) #todo: PAT.html

    return ("Only POST allowed.")

@app.route('/edit_external/<repo_key>/<path:folder_path>')
@verify_logged_in
def edit_external(repo_key, folder_path):
    # print("edit_external reached") 
    repositories = app.config['REPOSITORIES']
    repo_detail = repositories[repo_key]
    folder=folder_path
    spreadsheets = []
    directories = github.get(
        f'repos/{repo_detail}/contents/{folder_path}' 
    )   
    for directory in directories:
        spreadsheets.append(directory['name'])
    #todo: need unique name for each? Or do we append to big array? 
    # for spreadsheet in spreadsheets:
    #     print("spreadsheet: ", spreadsheet)
        
    sheet1, sheet2, sheet3 = spreadsheets
    (file_sha1,rows1,header1) = get_spreadsheet(repo_detail,folder,sheet1)
    # not a spreadsheet but a csv file:
    (file_sha2,rows2,header2) = get_csv(repo_detail,folder,sheet2) 
    (file_sha3,rows3,header3) = get_csv(repo_detail,folder,sheet3)
    return render_template('edit_external.html', 
                            login=g.user.github_login, 
                            repo_name = repo_key,
                            folder_path = folder_path,
                            spreadsheets=spreadsheets, #todo: delete, just for test
                            rows1=json.dumps(rows1),
                            rows2=json.dumps(rows2),
                            rows3=json.dumps(rows3)
                            )

@app.route('/save_new_ontology', methods=['POST'])
@verify_logged_in
def save_new_ontology():
    new_ontology = request.form.get("new_ontology")
    print("Received new Ontology: " + new_ontology)
    response = "test"
    return ( json.dumps({"message":"Success",
                             "response": response}), 200 )

@app.route('/update_ids', methods=['POST'])
@verify_logged_in
def update_ids():
    current_ontology=request.form.get("current_ontology")
    new_IDs=request.form.get("new_IDs")
    print("Received new IDs from : "+ current_ontology)
    print("data is: ", new_IDs)
    response = "test"
    return ( json.dumps({"message":"Success",
                             "response": response}), 200 )

# Internal methods

def get_csv(repo_detail,folder,spreadsheet):

    csv_file = github.get(
        f'repos/{repo_detail}/contents/{folder}/{spreadsheet}'
    )
    file_sha = csv_file['sha']
    csv_content = csv_file['content']
    # print(csv_content)
    decoded_data = str(base64.b64decode(csv_content),'utf-8')
    # print(decoded_data)
    csv_reader = csv.reader(io.StringIO(decoded_data))
    csv_data = list(csv_reader)
    header = csv_data[0:1]
    rows = csv_data[1:]
    
    # print(f'{spreadsheet} header: ', header)
    # print(f'{spreadsheet} rows: ', rows)

    return ( (file_sha, rows, header) )

def get_spreadsheet(repo_detail,folder,spreadsheet):
    spreadsheet_file = github.get(
        f'repos/{repo_detail}/contents/{folder}/{spreadsheet}'
    )
    file_sha = spreadsheet_file['sha']
    base64_bytes = spreadsheet_file['content'].encode('utf-8')
    decoded_data = base64.decodebytes(base64_bytes)
    wb = openpyxl.load_workbook(io.BytesIO(decoded_data))
    sheet = wb.active

    header = [cell.value for cell in sheet[1] if cell.value]
    rows = []
    for row in sheet[2:sheet.max_row]:
        values = {}
        for key, cell in zip(header, row):
            values[key] = cell.value
        if any(values.values()):
            rows.append(values)
    # print(f'rows: ')
    # print(json.dumps(rows))
    return ( (file_sha, rows, header) )


def getDiff(row_data_1, row_data_2, row_header, row_data_3): #(1saving, 2server, header, 3initial)

    # print(f'the type of row_data_3 is: ')
    # print(type(row_data_3))        

    #sort out row_data_1 format to be the same as row_data_2
    new_row_data_1 = []
    for k in row_data_1:
        dictT = {}
        for key, val, item in zip(k, k.values(), k.items()):
            if(key != "id"):
                if(val == ""):
                    val = None
                #add to dictionary:
                dictT[key] = val
        #add to list:
        new_row_data_1.append( dictT ) 

    #sort out row_data_3 format to be the same as row_data_2
    new_row_data_3 = []
    for h in row_data_3:
        dictT3 = {}
        for key, val, item in zip(h, h.values(), h.items()):
            if(key != "id"):
                if(val == ""):
                    val = None
                #add to dictionary:
                dictT3[key] = val
        #add to list:
        new_row_data_3.append( dictT3 ) 

    row_data_combo_1 = [row_header] 
    row_data_combo_2 = [row_header]
    row_data_combo_3 = [row_header]

    row_data_combo_1.extend([list(r.values()) for r in new_row_data_1]) #row_data_1 has extra "id" column for some reason???!!!
    row_data_combo_2.extend([list(s.values()) for s in row_data_2])
    row_data_combo_3.extend([list(t.values()) for t in new_row_data_3])

    #checking:
    # print(f'row_header: ')
    # print(row_header)
    # print(f'row_data_1: ')
    # print(row_data_1)
    # print(f'row_data_2: ')
    # print(row_data_2)
    # print(f'combined 1: ')
    # print(row_data_combo_1)
    # print(f'combined 2: ')
    # print(row_data_combo_2)
    # print(f'combined 3: ')
    # print(row_data_combo_3)

    table1 = daff.PythonTableView(row_data_combo_1) #daff needs a header in order to work correctly!
    table2 = daff.PythonTableView(row_data_combo_2)
    table3 = daff.PythonTableView(row_data_combo_3)
    
    #old version:
    # table1 = daff.PythonTableView([list(r.values()) for r in row_data_1])
    # table2 = daff.PythonTableView([list(r.values()) for r in row_data_2])

    alignment = daff.Coopy.compareTables3(table3,table2,table1).align() #3 way: initial vs server vs saving
    
    # alignment = daff.Coopy.compareTables(table3,table2).align() #initial vs server
    alignment2 = daff.Coopy.compareTables(table2,table1).align() #saving vs server
    # alignment2 = daff.Coopy.compareTables(table1, table2).align() #server vs saving
    # alignment = daff.Coopy.compareTables(table3,table1).align() #initial vs saving


    data_diff = []
    table_diff = daff.PythonTableView(data_diff)

    flags = daff.CompareFlags()

    # flags.allowDelete()
    # flags.allowUpdate()
    # flags.allowInsert()

    highlighter = daff.TableDiff(alignment2,flags)
    
    highlighter.hilite(table_diff)
    #hasDifference() should return true - and it does. 
    if highlighter.hasDifference():
        print(f'HASDIFFERENCE')
        print(highlighter.getSummary().row_deletes)
    else:
        print(f'no difference found')
    diff2html = daff.DiffRender()
    diff2html.usePrettyArrows(False)
    diff2html.render(table_diff)
    table_diff_html = diff2html.html()

    # print(table_diff_html)
    # print(f'table 1 before patch test: ')
    # print(table1.toString()) 
    # patch test: 
    # patcher = daff.HighlightPatch(table2,table_diff)
    # patcher.apply()
    # print(f'patch tester: ..................')
    # print(f'table1:')
    # print(table1.toString())
    # print(f'table2:')
    # print(table2.toString())
    # print(table2.toString()) 
    # print(f'table3:')
    # print(table3.toString()) 
    # table2String = table2.toString().strip() #no
    #todo: Task 1: turn MergeData into a Dict in order to post it to Github!
    # - use Janna's sheet builder example? 
    # - post direct instead of going through Flask front-end? 
    # table2String.strip()
    # table2Json = json.dumps(table2)
    # table2Dict = dict(todo: make this into a dict with id:0++ per row here!)
    # table2String = dict(table2String) #nope

    # merger test: 
    # print(f'Merger test: ') 
    merger = daff.Merger(table3,table2,table1,flags) #(3initial, 1saving, 2server, flags)
    merger.apply()
    # print(f'table2:')
    # table2String = table2.toString()
    # print(table2String) #after merger

    data = table2.getData() #merger table in list format
    # print(f'data: ')
    # print(json.dumps(data)) #it's a list.
    # convert to correct format (list of dicts):
    dataDict = []
    iter = -1
    for k in data:        
        # add "id" value:
        iter = iter + 1
        dictT = {}
        if iter == 0:
            pass
            # print(f'header row - not using')
        else:
            dictT['id'] = iter # add "id" with iteration
            for key, val in zip(row_header, k):      
                #deal with conflicting val?

                dictT[key] = val
        # add to list:
        if iter > 0: # not header - now empty dict
            dataDict.append( dictT ) 
        # print(f'update: ')
        # print(dataDict)

        
    
    # print(f'dataDict: ')
    # print(json.dumps(dataDict))
    # print(f'the type of dataDict is: ')
    # print(type(dataDict))

    # print(f'merger data:') #none
    # print(daff.DiffSummary().different) #nothing here? 
    # mergerConflictInfo = merger.getConflictInfos()
    
    # print(f'Merger conflict infos: ')
    # print(f'table1:')
    # print(table1.toString())
    # print(f'table2:')
    # print(table2.toString()) 
    # print(f'table3:')
    # print(table3.toString()) 
   
    return (table_diff_html, dataDict)


def searchAcrossSheets(repo_name, search_string):
    searcherAllResults = searcher.searchFor(repo_name, search_string=search_string)
    # print(searcherAllResults)
    return searcherAllResults

def searchAssignedTo(repo_name, initials):
    searcherAllResults = searcher.searchFor(repo_name, assigned_user=initials)
    # print(searcherAllResults)
    return searcherAllResults


if __name__ == "__main__":        # on running python app.py

    app.run(debug=app.config["DEBUG"], port=8080)        # run the flask app



# [END gae_python37_app]
