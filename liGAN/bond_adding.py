import sys, os, pickle
from openbabel import openbabel as ob
from openbabel import pybel
from rdkit import Chem, Geometry
import numpy as np
from scipy.spatial.distance import pdist, squareform

from .atom_types import Atom
from .molecules import ob_mol_to_rd_mol, Molecule


class BondAdder(object):
    '''
    An algorithm for constructing a valid molecule
    from a structure of atomic coordinates and types.

    First, it converts the struture to OBAtoms and
    tries to maintain as many of the atomic proper-
    ties defined by the atom types as possible.

    Next, it add bonds to the atoms, using the atom
    properties and coordinates as constraints.
    '''
    def __init__(
        self,
        min_bond_len=0.01,
        max_bond_len=4.0,
        max_bond_stretch=0.45,
        min_bond_angle=45,
    ):
        self.min_bond_len = min_bond_len
        self.max_bond_len = max_bond_len

        self.max_bond_stretch = max_bond_stretch
        self.min_bond_angle = min_bond_angle

    def set_aromaticity(self, ob_mol, atoms, struct):
        '''
        Set aromaticiy of atoms based on their atom
        types. Aromatic atoms are also marked as
        having sp2 hybridization. Bonds are set as
        aromatic iff both atoms are aromatic.
        '''
        if Atom.aromatic not in struct.typer:
            return False

        for ob_atom, atom_type in zip(atoms, struct.atom_types):

            if atom_type.aromatic:
                ob_atom.SetAromatic(True)
                ob_atom.SetHyb(2)
            else:
                ob_atom.SetAromatic(False)

        for bond in ob.OBMolBondIter(ob_mol):
            a1 = bond.GetBeginAtom()
            a2 = bond.GetEndAtom()
            bond.SetAromatic(a1.IsAromatic() and a2.IsAromatic())

        ob_mol.SetAromaticPerceived(True)
        return True

    def set_formal_charges(self, ob_mol, atoms, struct):
        '''
        Set formal charge on atoms based on their
        atom type, if it is available.
        '''
        if Atom.formal_charge not in struct.typer:
            return False

        for ob_atom, atom_type in zip(atoms, struct.atom_types):
            ob_atom.SetFormalCharge(atom_type.formal_charge)

        return True

    def set_min_h_counts(self, ob_mol, atoms, struct):
        '''
        Set atoms to have at least one H if they are
        hydrogen bond donors, or the exact number of
        Hs specified by their atom type, if it is
        available.
        '''
        assert not ob_mol.HasHydrogensAdded()

        for ob_atom, atom_type in zip(atoms, struct.atom_types):
            #assert ob_atom.GetExplicitDegree() == ob_atom.GetHvyDegree()

            if 'h_degree' in atom_type._fields:
                ob_atom.SetImplicitHCount(atom_type.h_degree)

            elif 'h_donor' in atom_type._fields:
                if atom_type.h_donor and ob_atom.GetImplicitHCount() == 0:
                    ob_atom.SetImplicitHCount(1)

    def set_rem_h_counts(self, ob_mol, atoms, struct):
        '''
        Set atoms with empty valences to have up to
        the maximum number of hydrogens allowed by
        their atom type- or set the exact number of
        Hs, if it is avalable.
        '''
        assert ob_mol.HasHydrogensAdded() # no implicit Hs
        ob_mol.SetHydrogensAdded(False)

        for ob_atom, atom_type in zip(atoms, struct.atom_types):
            assert ob_atom.GetImplicitHCount() == 0
            h_count = ob_atom.GetImplicitHCount()

            if 'h_degree' in atom_type._fields:
                pass # assume these have been set previously

            else:
                # need to set charge before AssignTypicalImplicitHs
                if 'formal_charge' in atom_type._fields:
                    ob_atom.SetFormalCharge(atom_type.formal_charge)

                # this uses explicit valence and formal charge
                ob.OBAtomAssignTypicalImplicitHydrogens(ob_atom)
                typical_h_count = ob_atom.GetImplicitHCount()

                if typical_h_count > h_count: # only ever increase
                    h_count = typical_h_count

                ob_atom.SetImplicitHCount(h_count)

    def add_within_distance(self, ob_mol, atoms, struct):

        # just do n^2 comparisons, worry about efficiency later
        coords = np.array([(a.GetX(), a.GetY(), a.GetZ()) for a in atoms])
        dists = squareform(pdist(coords))

        # add bonds between every atom pair within a certain distance
        for i, atom_a in enumerate(atoms):
            for j, atom_b in enumerate(atoms):
                if i >= j: # avoid redundant checks
                    continue

                # if distance is between min and max bond length,
                if self.min_bond_len < dists[i,j] < self.max_bond_len:

                    # add single bond
                    ob_mol.AddBond(atom_a.GetIdx(), atom_b.GetIdx(), 1)

    def remove_bad_valences(self, ob_mol, atoms, struct):

        # get max valence of the atom types
        max_vals = get_max_valences(atoms, struct)

        # remove any impossible bonds between halogens (mtr22- and hydrogens)
        for bond in ob.OBMolBondIter(ob_mol):
            atom_a = bond.GetBeginAtom()
            atom_b = bond.GetEndAtom()
            if (
                max_vals[atom_a.GetIdx()] == 1 and
                max_vals[atom_b.GetIdx()] == 1
            ):
                ob_mol.DeleteBond(bond)

        # removing bonds causing larger-than-permitted valences
        # prioritize atoms with lowest max valence, since they tend
        # to introduce the most problems with reachability (e.g O)

        atom_info = sort_atoms_by_valence(atoms, max_vals)
        for max_val, rem_val, atom in atom_info:

            if atom.GetExplicitValence() <= max_val:
                continue
            # else, the atom could have an invalid valence
            # so check whether we can remove a bond

            bond_info = sort_bonds_by_stretch(ob.OBAtomBondIter(atom))
            for bond_stretch, bond_len, bond in bond_info:
                atom1 = bond.GetBeginAtom()
                atom2 = bond.GetEndAtom()

                # check whether valences are not permitted (this could
                # have changed since the call to sort_atoms_by_valence)
                if atom1.GetExplicitValence() > max_vals[atom1.GetIdx()] or \
                    atom2.GetExplicitValence() > max_vals[atom2.GetIdx()]:
            
                    if reachable(atom1, atom2): # don't fragment the molecule
                        ob_mol.DeleteBond(bond)

                    # if the current atom now has a permitted valence,
                    # break and let other atoms choose next bonds to remove
                    if atom.GetExplicitValence() <= max_vals[atom.GetIdx()]:
                        break

    def remove_bad_geometry(self, ob_mol):

        # eliminate geometrically poor bonds
        bond_info = sort_bonds_by_stretch(ob.OBMolBondIter(ob_mol))
        for bond_stretch, bond_len, bond in bond_info:

            # can we remove this bond without disconnecting the molecule?
            atom1 = bond.GetBeginAtom()
            atom2 = bond.GetEndAtom()

            # as long as we aren't disconnecting, let's remove things
            # that are excessively far away (0.45 from ConnectTheDots)
            # get bonds to be less than max allowed
            # also remove tight angles, as done in openbabel
            if (bond_stretch > self.max_bond_stretch
                or forms_small_angle(atom1, atom2, self.min_bond_angle)
                or forms_small_angle(atom2, atom1, self.min_bond_angle)
            ):
                if reachable(atom1, atom2): # don't fragment the molecule
                    ob_mol.DeleteBond(bond)

    def add_bonds(self, ob_mol, atoms, struct):

        # track each step of bond adding
        visited_mols = [ob.OBMol(ob_mol)]

        if len(atoms) == 0: # nothing to do
            return ob_mol, visited_mols

        ob_mol.BeginModify()

        # add all bonds between atom pairs within a distance range
        self.add_within_distance(ob_mol, atoms, struct)
        visited_mols.append(ob.OBMol(ob_mol))

        # set minimum H counts to determine hyper valency
        #   but don't make them explicit yet to avoid issues
        #   with bond adding/removal (i.e. ignore H bonds)
        self.set_min_h_counts(ob_mol, atoms, struct)
        visited_mols.append(ob.OBMol(ob_mol))

        # remove bonds to atoms that are above their allowed valence
        #   with priority towards removing highly stretched bonds
        self.remove_bad_valences(ob_mol, atoms, struct)
        visited_mols.append(ob.OBMol(ob_mol))

        # remove bonds whose lengths/angles are excessively distorted
        self.remove_bad_geometry(ob_mol)
        visited_mols.append(ob.OBMol(ob_mol))

        # NOTE the next section is important, but not intuitive,
        #   and the order of operations deserves explanation:
        # need to AddHydrogens() before PerceiveBondOrders()
        #   bc it fills remaining EXPLICIT valence with bonds
        # need to EndModify() before PerceiveBondOrders()
        #   otherwise you get a segmentation fault
        # need to AddHydrogens() after EndModify()
        #   because EndModify() resets hydrogen coords
        # need to set_aromaticity() before AddHydrogens()
        #   bc it uses hybridization to create H coords
        # need to set_aromaticity() before AND after EndModify()
        #   otherwise aromatic atom types are missing

        self.set_aromaticity(ob_mol, atoms, struct)
        ob_mol.EndModify()
        self.set_aromaticity(ob_mol, atoms, struct)
        visited_mols.append(ob.OBMol(ob_mol))

        ob_mol.AddHydrogens()
        ob_mol.PerceiveBondOrders()
        visited_mols.append(ob.OBMol(ob_mol))

        # fill remaining valences with h bonds,
        #   up to max num allowed by the atom types
        #   also set formal charge, if available
        self.set_formal_charges(ob_mol, atoms, struct)
        self.set_rem_h_counts(ob_mol, atoms, struct)
        ob_mol.AddHydrogens()
        visited_mols.append(ob.OBMol(ob_mol))

        return ob_mol, visited_mols

    def post_process_rd_mol(self, rd_mol, struct=None):
        '''
        Convert OBMol to RDKit mol, fixing up issues.
        '''
        pt = Chem.GetPeriodicTable()
        #if double/triple bonds are connected to hypervalent atoms, decrement the order

        positions = rd_mol.GetConformer().GetPositions()
        nonsingles = []
        for bond in rd_mol.GetBonds():
            if bond.GetBondType() == Chem.BondType.DOUBLE or bond.GetBondType() == Chem.BondType.TRIPLE:
                i = bond.GetBeginAtomIdx()
                j = bond.GetEndAtomIdx()
                dist = np.linalg.norm(positions[i]-positions[j])
                nonsingles.append((dist,bond))
        nonsingles.sort(reverse=True, key=lambda t: t[0])

        for (d,bond) in nonsingles:
            a1 = bond.GetBeginAtom()
            a2 = bond.GetEndAtom()

            if calc_valence(a1) > pt.GetDefaultValence(a1.GetAtomicNum()) or \
               calc_valence(a2) > pt.GetDefaultValence(a2.GetAtomicNum()):
                btype = Chem.BondType.SINGLE
                if bond.GetBondType() == Chem.BondType.TRIPLE:
                    btype = Chem.BondType.DOUBLE
                bond.SetBondType(btype)

        for atom in rd_mol.GetAtoms():
            #set nitrogens with 4 neighbors to have a charge
            if atom.GetAtomicNum() == 7 and atom.GetDegree() == 4:
                atom.SetFormalCharge(1)

        rd_mol = Chem.AddHs(rd_mol,addCoords=True)

        positions = rd_mol.GetConformer().GetPositions()
        center = np.mean(positions[np.all(np.isfinite(positions),axis=1)],axis=0)
        for atom in rd_mol.GetAtoms():
            i = atom.GetIdx()
            pos = positions[i]
            if not np.all(np.isfinite(pos)):
                #hydrogens on C fragment get set to nan (shouldn't, but they do)
                rd_mol.GetConformer().SetAtomPosition(i,center)

        try:
            Chem.SanitizeMol(rd_mol,Chem.SANITIZE_ALL^Chem.SANITIZE_KEKULIZE)
        except: # mtr22 - don't assume mols will pass this
            pass
            # dkoes - but we want to make failures as rare as possible and should debug them
            m = pybel.Molecule(ob_mol)
            if not os.path.isdir('badmols'):
                os.mkdir('badmols')
            i = np.random.randint(1000000)
            outname = 'badmols/badmol%d.sdf'%i
            print("WRITING", outname, file=sys.stderr)
            m.write('sdf',outname,overwrite=True)
            if struct:
                pickle.dump(struct,open('badmols/badmol%d.pkl'%i,'wb'))

        #but at some point stop trying to enforce our aromaticity -
        #openbabel and rdkit have different aromaticity models so they
        #won't always agree.  Remove any aromatic bonds to non-aromatic atoms
        for bond in rd_mol.GetBonds():
            a1 = bond.GetBeginAtom()
            a2 = bond.GetEndAtom()
            if bond.GetIsAromatic():
                if not a1.GetIsAromatic() or not a2.GetIsAromatic():
                    bond.SetIsAromatic(False)
            elif a1.GetIsAromatic() and a2.GetIsAromatic():
                bond.SetIsAromatic(True)

        return rd_mol

    def make_mol(self, struct):
        '''
        Create a Molecule from an AtomStruct with added
        bonds, trying to maintain the same atom types.
        '''
        ob_mol, atoms = struct.to_ob_mol()
        ob_mol, visited_mols = self.add_bonds(ob_mol, atoms, struct)
        add_mol = Molecule.from_ob_mol(ob_mol)
        add_struct = struct.typer.make_struct(add_mol.to_ob_mol())
        visited_mols = [
            Molecule.from_ob_mol(m) for m in visited_mols
        ] + [add_mol]
        return add_mol, add_struct, visited_mols


def calc_valence(rd_atom):
    '''
    Can call GetExplicitValence before sanitize,
    but need to know this to fix up the molecule
    to prevent sanitization failures.
    '''
    val = 0
    for bond in rd_atom.GetBonds():
        val += bond.GetBondTypeAsDouble()
    return val


def reachable_r(atom_a, atom_b, visited_bonds):
    '''
    Recursive helper for determining whether
    atom_a is reachable from atom_b without
    using the bond between them.
    '''
    for nbr in ob.OBAtomAtomIter(atom_a):
        bond = atom_a.GetBond(nbr).GetIdx()
        if bond not in visited_bonds:
            visited_bonds.add(bond)
            if nbr == atom_b:
                return True
            elif reachable_r(nbr, atom_b, visited_bonds):
                return True
    return False


def reachable(atom_a, atom_b):
    '''
    Return true if atom b is reachable from a
    without using the bond between them.
    '''
    if atom_a.GetExplicitDegree() == 1 or atom_b.GetExplicitDegree() == 1:
        return False # this is the _only_ bond for one atom

    # otherwise do recursive traversal
    visited_bonds = set([atom_a.GetBond(atom_b).GetIdx()])
    return reachable_r(atom_a, atom_b, visited_bonds)


def forms_small_angle(atom_a, atom_b, cutoff=45):
    '''
    Return whether bond between atom_a and atom_b
    is part of a small angle with a neighbor of a
    only.
    '''
    for nbr in ob.OBAtomAtomIter(atom_a):
        if nbr != atom_b:
            degrees = atom_b.GetAngle(atom_a, nbr)
            if degrees < cutoff:
                return True
    return False


def sort_bonds_by_stretch(bonds):
    '''
    Return bonds sorted by their distance
    from the optimal covalent bond length.
    '''
    bond_info = []
    for bond in bonds:

        # compute how far away from optimal we are
        atomic_num1 = bond.GetBeginAtom().GetAtomicNum()
        atomic_num2 = bond.GetEndAtom().GetAtomicNum()
        ideal_bond_len = (
            ob.GetCovalentRad(atomic_num1) +
            ob.GetCovalentRad(atomic_num2)
        )
        bond_len = bond.GetLength()
        stretch = np.abs(bond_len - ideal_bond_len) # mtr22- take abs
        bond_info.append((stretch, bond_len, bond))

    # sort bonds from most to least stretched
    bond_info.sort(reverse=True, key=lambda t: (t[0], t[1]))
    return bond_info


def count_nbrs_of_elem(atom, atomic_num):
    count = 0
    for nbr in ob.OBAtomAtomIter(atom):
        if nbr.GetAtomicNum() == atomic_num:
            count += 1
    return count


def get_max_valences(atoms, struct):

    # determine max allowed valences
    max_vals = {}
    for i, ob_atom in enumerate(atoms):

        # set max valance to the smallest allowed by either openbabel
        # or rdkit, since we want the molecule to be valid for both
        # (rdkit is usually lower, mtr22- specifically for N, 3 vs 4)
        atomic_num = ob_atom.GetAtomicNum()
        max_val = min(
            ob.GetMaxBonds(atomic_num),
            Chem.GetPeriodicTable().GetDefaultValence(atomic_num)
        )
        atom_type = struct.typer.get_atom_type(struct.types[i])

        if atom_type.atomic_num == 16: # sulfone check
            if count_nbrs_of_elem(ob_atom, 8) >= 2:
                max_val = 6

        if Atom.formal_charge in struct.typer:
            max_val += atom_type.formal_charge #mtr22- is this correct?

        if Atom.h_degree in struct.typer:
            max_val -= atom_type.h_degree # leave room for hydrogen

        elif Atom.h_donor in struct.typer:
            if atom_type.h_donor:
                max_val -= 1  # leave room for hydrogen (mtr22- how many?)

        max_vals[ob_atom.GetIdx()] = max_val

    return max_vals


def sort_atoms_by_valence(atoms, max_vals):
    '''
    Return atoms sorted by their explicit 
    valence and difference from maximum
    allowed valence.
    '''
    atom_info = []
    for atom in atoms:
        max_val = max_vals[atom.GetIdx()]
        rem_val = max_val - atom.GetExplicitValence()
        atom_info.append((max_val, rem_val, atom))

        # mtr22- should we sort by rem_val first instead?
        # doesn't this mean that we will always choose to
        # remove a bond to a low max-valence atom, even if it
        # has remaining valence, and even if there are higher
        # max-valence atom that have less remaining valence?

    # sort atoms from least to most remaining valence
    atom_info.sort(key=lambda t: (t[0], t[1]))
    return atom_info