import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcode;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.formatter.DexFormatter;
import com.android.tools.smali.dexlib2.iface.*;
import com.android.tools.smali.dexlib2.iface.instruction.*;
import com.android.tools.smali.dexlib2.iface.instruction.formats.ArrayPayload;
import com.android.tools.smali.dexlib2.iface.instruction.formats.UnknownInstruction;
import java.io.File;
import java.util.*;

/** Narrow semantic check of the single injected SystemUI callback; never rewrites an APK. */
public final class VerifyDex {
    private static final String TARGET = "Lcom/android/systemui/statusbar/CommandQueue;->onSystemBarAttributesChanged(II[Lcom/android/internal/view/AppearanceRegion;ZIILjava/lang/String;[Lcom/android/internal/statusbar/LetterboxDetails;)V";
    private static final String BRIDGE = "Lcom/buildploy/statusbarprototype/AppearanceBridge;->dispatch(IILjava/lang/Object;ZIILjava/lang/String;Ljava/lang/Object;)V";
    private static final DexFormatter FORMAT = DexFormatter.INSTANCE;
    private static long classes, fields, methods, instructions, tries;
    private static int matched;

    private static void same(Object before, Object after, String where) {
        if (!Objects.equals(before, after)) throw new IllegalStateException(where + " differs: original=" + before + ", candidate=" + after);
    }
    private static void require(boolean condition, String message) {
        if (!condition) throw new IllegalStateException(message);
    }
    private static String methodKey(Method method) { return FORMAT.getMethodDescriptor(method); }
    private static Map<String, ClassDef> classes(DexFile dex) {
        Map<String, ClassDef> result = new TreeMap<>();
        for (ClassDef item : dex.getClasses()) require(result.put(item.getType(), item) == null, "Duplicate class " + item.getType());
        return result;
    }
    private static Map<String, Method> methods(Iterable<? extends Method> values) {
        Map<String, Method> result = new TreeMap<>();
        for (Method item : values) require(result.put(methodKey(item), item) == null, "Duplicate method " + methodKey(item));
        return result;
    }
    private static Map<String, Field> fields(Iterable<? extends Field> values) {
        Map<String, Field> result = new TreeMap<>();
        for (Field item : values) require(result.put(FORMAT.getFieldDescriptor(item), item) == null, "Duplicate field " + FORMAT.getFieldDescriptor(item));
        return result;
    }
    private static List<Instruction> code(MethodImplementation impl) {
        List<Instruction> result = new ArrayList<>();
        for (Instruction instruction : impl.getInstructions()) result.add(instruction);
        return result;
    }

    // Symbolic references ignore pool indices, which legitimately change when a DEX is rewritten.
    private static List<Object> instruction(Instruction i) {
        List<Object> values = new ArrayList<>();
        values.add(i.getOpcode()); values.add(i.getCodeUnits());
        if (i instanceof OneRegisterInstruction) values.add("a=" + ((OneRegisterInstruction) i).getRegisterA());
        if (i instanceof TwoRegisterInstruction) values.add("b=" + ((TwoRegisterInstruction) i).getRegisterB());
        if (i instanceof ThreeRegisterInstruction) values.add("c=" + ((ThreeRegisterInstruction) i).getRegisterC());
        if (i instanceof VariableRegisterInstruction) values.add("count=" + ((VariableRegisterInstruction) i).getRegisterCount());
        if (i instanceof RegisterRangeInstruction) values.add("start=" + ((RegisterRangeInstruction) i).getStartRegister());
        if (i instanceof FiveRegisterInstruction) {
            FiveRegisterInstruction f = (FiveRegisterInstruction) i;
            int[] registers = {f.getRegisterC(), f.getRegisterD(), f.getRegisterE(), f.getRegisterF(), f.getRegisterG()};
            for (int n = 0; n < f.getRegisterCount(); n++) values.add("arg" + n + "=" + registers[n]);
        }
        if (i instanceof WideLiteralInstruction) values.add("literal=" + ((WideLiteralInstruction) i).getWideLiteral());
        if (i instanceof OffsetInstruction) values.add("offset=" + ((OffsetInstruction) i).getCodeOffset());
        if (i instanceof ReferenceInstruction) {
            ReferenceInstruction r = (ReferenceInstruction) i;
            values.add("referenceType=" + r.getReferenceType()); values.add(FORMAT.getReference(r.getReference()));
        }
        if (i instanceof DualReferenceInstruction) {
            DualReferenceInstruction r = (DualReferenceInstruction) i;
            values.add("referenceType2=" + r.getReferenceType2()); values.add(FORMAT.getReference(r.getReference2()));
        }
        if (i instanceof VerificationErrorInstruction) values.add("verification=" + ((VerificationErrorInstruction) i).getVerificationError());
        if (i instanceof FieldOffsetInstruction) values.add("fieldOffset=" + ((FieldOffsetInstruction) i).getFieldOffset());
        if (i instanceof VtableIndexInstruction) values.add("vtableIndex=" + ((VtableIndexInstruction) i).getVtableIndex());
        if (i instanceof InlineIndexInstruction) values.add("inlineIndex=" + ((InlineIndexInstruction) i).getInlineIndex());
        if (i instanceof UnknownInstruction) values.add("originalOpcode=" + ((UnknownInstruction) i).getOriginalOpcode());
        if (i instanceof SwitchPayload) for (SwitchElement e : ((SwitchPayload) i).getSwitchElements()) values.add("switch=" + e.getKey() + ":" + e.getOffset());
        if (i instanceof ArrayPayload) {
            ArrayPayload p = (ArrayPayload) i;
            values.add("elementWidth=" + p.getElementWidth());
            long mask = p.getElementWidth() == 8 ? -1L : (1L << (p.getElementWidth() * 8)) - 1;
            for (Number n : p.getArrayElements()) values.add(n.longValue() & mask);
        }
        return values;
    }

    private static List<Object> tryBlocks(MethodImplementation impl, int shift) {
        List<Object> result = new ArrayList<>();
        for (TryBlock<? extends ExceptionHandler> block : impl.getTryBlocks()) {
            List<Object> item = new ArrayList<>();
            item.add(block.getStartCodeAddress() - shift); item.add(block.getCodeUnitCount());
            for (ExceptionHandler handler : block.getExceptionHandlers())
                item.add(Arrays.asList(handler.getExceptionType(), handler.getHandlerCodeAddress() - shift));
            result.add(item);
        }
        return result;
    }

    private static void method(Method before, Method after) {
        String key = methodKey(before);
        same(before.getAccessFlags(), after.getAccessFlags(), key + " flags");
        same(before.getAnnotations(), after.getAnnotations(), key + " annotations");
        same(before.getHiddenApiRestrictions(), after.getHiddenApiRestrictions(), key + " hidden API restrictions");
        for (int p = 0; p < before.getParameters().size(); p++) {
            MethodParameter a = before.getParameters().get(p), b = after.getParameters().get(p);
            same(a.getName(), b.getName(), key + " parameter name " + p);
            same(a.getSignature(), b.getSignature(), key + " parameter signature " + p);
            same(a.getAnnotations(), b.getAnnotations(), key + " parameter annotations " + p);
        }
        MethodImplementation a = before.getImplementation(), b = after.getImplementation();
        same(a == null, b == null, key + " implementation presence");
        methods++;
        if (a == null) return;
        same(a.getRegisterCount(), b.getRegisterCount(), key + " register count");
        List<Instruction> oldCode = code(a), newCode = code(b);
        int prefix = 0, shift = 0;
        if (TARGET.equals(key)) {
            matched++;
            same(11, a.getRegisterCount(), "Pinned target register count");
            same(22, oldCode.size(), "Pinned target instruction count");
            require(!newCode.isEmpty(), "Missing target prefix");
            Instruction first = newCode.get(0);
            same(Opcode.INVOKE_STATIC_RANGE, first.getOpcode(), "Target prefix opcode");
            same(3, first.getCodeUnits(), "Target prefix code units");
            require(first instanceof RegisterRangeInstruction && first instanceof ReferenceInstruction, "Target prefix format");
            same(3, ((RegisterRangeInstruction) first).getStartRegister(), "Target prefix start register");
            same(8, ((RegisterRangeInstruction) first).getRegisterCount(), "Target prefix argument count");
            same(BRIDGE, FORMAT.getReference(((ReferenceInstruction) first).getReference()), "Target prefix method reference");
            prefix = 1; shift = 3;
        }
        same(oldCode.size() + prefix, newCode.size(), key + " instruction count");
        for (int n = 0; n < oldCode.size(); n++) {
            same(instruction(oldCode.get(n)), instruction(newCode.get(n + prefix)), key + " instruction " + n);
            instructions++;
        }
        same(tryBlocks(a, 0), tryBlocks(b, shift), key + " try boundaries and handlers");
        tries += a.getTryBlocks().size();
    }

    private static void compare(ClassDef before, ClassDef after) {
        String key = before.getType();
        same(before.getAccessFlags(), after.getAccessFlags(), key + " flags");
        same(before.getSuperclass(), after.getSuperclass(), key + " superclass");
        same(before.getInterfaces(), after.getInterfaces(), key + " interfaces");
        same(before.getSourceFile(), after.getSourceFile(), key + " source file");
        same(before.getAnnotations(), after.getAnnotations(), key + " annotations");
        Map<String, Field> aFields = fields(before.getFields()), bFields = fields(after.getFields());
        same(aFields.keySet(), bFields.keySet(), key + " field signatures");
        for (String name : aFields.keySet()) {
            Field a = aFields.get(name), b = bFields.get(name);
            same(a.getAccessFlags(), b.getAccessFlags(), name + " flags");
            same(a.getInitialValue(), b.getInitialValue(), name + " initial value");
            same(a.getAnnotations(), b.getAnnotations(), name + " annotations");
            same(a.getHiddenApiRestrictions(), b.getHiddenApiRestrictions(), name + " hidden API restrictions");
            fields++;
        }
        Map<String, Method> aMethods = methods(before.getMethods()), bMethods = methods(after.getMethods());
        same(aMethods.keySet(), bMethods.keySet(), key + " method signatures");
        same(methods(before.getDirectMethods()).keySet(), methods(after.getDirectMethods()).keySet(), key + " direct methods");
        same(methods(before.getVirtualMethods()).keySet(), methods(after.getVirtualMethods()).keySet(), key + " virtual methods");
        for (String name : aMethods.keySet()) method(aMethods.get(name), bMethods.get(name));
        classes++;
    }

    public static void main(String[] args) throws Exception {
        require(args.length == 2, "Usage: VerifyDex <original-SystemUI.apk> <patched-SystemUI.apk>");
        try {
            MultiDexContainer<?> original = DexFileFactory.loadDexContainer(new File(args[0]), Opcodes.forApi(35));
            MultiDexContainer<?> candidate = DexFileFactory.loadDexContainer(new File(args[1]), Opcodes.forApi(35));
            require(original.getEntry("classes2.dex") != null && candidate.getEntry("classes2.dex") != null, "Both APKs must contain classes2.dex");
            Map<String, ClassDef> a = classes(original.getEntry("classes2.dex").getDexFile());
            Map<String, ClassDef> b = classes(candidate.getEntry("classes2.dex").getDexFile());
            same(a.keySet(), b.keySet(), "classes2.dex original class set");
            for (String name : a.keySet()) compare(a.get(name), b.get(name));
            same(1, matched, "Expected exactly one modified method");
            System.out.println("PASS classes2.dex classes=" + classes + " fields=" + fields + " methods=" + methods
                    + " originalInstructions=" + instructions + " tryBlocks=" + tries
                    + " targetOriginalInstructions=22 prefixInstructions=1 prefixCodeUnits=3 targetRegisters=11");
            System.out.println("SCOPE original classes/fields/methods, flags, annotations, parameter metadata, register counts, symbolic operands/references/payloads and exception boundaries; target addresses normalized by +3. Debug items and other DEX ZIP entries are outside this tool.");
        } catch (Exception failure) {
            System.err.println("FAIL VerifyDex after classes=" + classes + " methods=" + methods + ": " + failure.getMessage());
            throw failure;
        }
    }
}
